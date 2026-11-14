"""Secret storage and the outbound request policy (§15)."""

import http.server
import threading

import pytest

from docwizard import netpolicy, secretbox
from docwizard.secretbox import SecretBox, SecretError


def test_seal_and_open_bind_the_row():
    box = SecretBox(secretbox._parse(secretbox.new_key_line()))
    sealed = box.seal("app-password", "sync_targets:1")
    assert "app-password" not in sealed and sealed.startswith("v1.")
    assert box.open(sealed, "sync_targets:1") == "app-password"
    with pytest.raises(SecretError):
        box.open(sealed, "sync_targets:2")  # moved to another row
    tampered = sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB")
    with pytest.raises(SecretError):
        box.open(tampered, "sync_targets:1")


def test_old_keys_still_open_after_rotation():
    old_line = secretbox.new_key_line()
    old = SecretBox(secretbox._parse(old_line))
    sealed = old.seal("token")
    new = SecretBox(secretbox._parse(secretbox.new_key_line() + "\n" + old_line))
    assert new.open(sealed) == "token" and new.needs_rotation(sealed)
    assert not new.needs_rotation(new.seal("token"))


def test_rotate_reencrypts_columns(cfg):
    from docwizard.db.database import Database

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path, "full")
    db.open()
    try:
        box = SecretBox.from_config(cfg)
        assert (cfg.data_dir / "secret.key").stat().st_mode & 0o777 == 0o600
        db.write_sync(lambda c: c.execute(
            "INSERT INTO users(id, username, display_name) VALUES (1, 'u', 'U')"))
        db.write_sync(lambda c: c.execute(
            "INSERT INTO projects(id, owner_id, name, layout_id) VALUES ('p1', 1, 'P', 'default')"))
        db.write_sync(lambda c: c.execute(
            "INSERT INTO sync_targets(id, project_id, kind, base_url, remote_path, username, secret_enc)"
            " VALUES (1, 'p1', 'webdav', 'https://dav.example', '/', 'u', ?)",
            (box.seal("pw", "sync_targets:1"),)))
        assert secretbox.rotate(cfg, db) == 1
        fresh = SecretBox.from_config(cfg)
        value = db.read_sync(lambda c: c.execute("SELECT secret_enc FROM sync_targets").fetchone()[0])
        assert not fresh.needs_rotation(value) and fresh.open(value, "sync_targets:1") == "pw"
    finally:
        db.close()


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/redirect-private":
            self.send_response(302)
            self.send_header("Location", "http://10.0.0.1/")
            self.end_headers()
            return
        if self.path == "/big":
            body = b"x" * 5000
        else:
            body = f"host={self.headers['Host']} auth={self.headers.get('Authorization')}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def local_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


def test_private_addresses_are_refused(local_server):
    policy = netpolicy.Policy()
    for url in (f"http://127.0.0.1:{local_server}/", "http://169.254.169.254/latest/meta-data",
                "http://[::1]/", "http://10.1.2.3/", "http://[::ffff:127.0.0.1]/", "http://100.64.0.1/"):
        with pytest.raises(netpolicy.PolicyError):
            policy.fetch("GET", url)
    with pytest.raises(netpolicy.PolicyError):
        policy.fetch("GET", "file:///etc/passwd")
    with pytest.raises(netpolicy.PolicyError):
        policy.fetch("GET", "http://user:pw@example.org/")


def test_allowed_hosts_and_pinned_connection(local_server):
    # "nextcloud.lan" doesn't exist in DNS: the request only works because the
    # connection goes to the address the policy checked, with the original Host header.
    resolver = lambda host, port: ["127.0.0.1"]  # noqa: E731
    policy = netpolicy.Policy(["nextcloud.lan"], resolver=resolver)
    r = policy.fetch("GET", f"http://nextcloud.lan:{local_server}/", headers={"Authorization": "Basic x"})
    assert r.status == 200 and r.text() == f"host=nextcloud.lan:{local_server} auth=Basic x"
    # Networks can be allowed too.
    assert netpolicy.Policy(["127.0.0.0/8"]).fetch("GET", f"http://127.0.0.1:{local_server}/").status == 200
    # Size limits.
    with pytest.raises(netpolicy.FetchError):
        policy.fetch("GET", f"http://nextcloud.lan:{local_server}/big", max_bytes=1000)
    # Redirects are checked again.
    with pytest.raises(netpolicy.PolicyError):
        policy.fetch("GET", f"http://nextcloud.lan:{local_server}/redirect-private")


def test_one_bad_address_refuses_the_host():
    policy = netpolicy.Policy(resolver=lambda host, port: ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(netpolicy.PolicyError):
        policy.check("mixed.example", 443)
