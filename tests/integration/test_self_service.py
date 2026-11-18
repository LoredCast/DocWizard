"""Self-service for an account: sessions, files and trash, export and deletion (§13.7, §7.7)."""

import io
import json
import re
import zipfile

from docwizard.pages.lifecycle import device_label
from tests.asgi_client import Client
from tests.integration.test_collab import new_project, open_editor
from tests.integration.test_sharing import USER_PASSWORD, make_user

FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko)"
          " Version/17.5 Mobile/15E148 Safari/604.1")


async def csrf_of(client, path="/settings"):
    page = await client.get(path)
    return re.search(r'name="_csrf" value="([^"]+)"', page.text).group(1)


async def login_from(app, username, password, ua):
    cl = Client(app)
    r = await cl.post("/login", form={"username": username, "password": password, "next": "/"},
                      headers={"user-agent": ua})
    assert r.status == 303, r.text
    return cl


def test_device_labels():
    assert device_label(FIREFOX) == "Firefox on Linux"
    assert device_label(IPHONE) == "Safari on iPhone"
    assert device_label("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
                        " Chrome/126.0 Safari/537.36 Edg/126.0") == "Edge on Windows"
    assert device_label("") == "Unknown device"


async def test_sessions_list_and_revoke(app, client):
    olga = await make_user(app, "olga")
    phone = await login_from(app, "olga", USER_PASSWORD, IPHONE)
    laptop = await login_from(app, "olga", USER_PASSWORD, FIREFOX)
    page = await laptop.get("/settings")
    assert "Safari on iPhone" in page.text and "Firefox on Linux" in page.text
    assert page.text.count("this device") == 1
    phone_id = re.search(r'Safari on iPhone.*?name="id" value="([0-9a-f]{16})"', page.text, re.S).group(1)
    csrf = await csrf_of(laptop)
    r = await laptop.post("/settings/sessions/revoke", form={"_csrf": csrf, "id": phone_id})
    assert r.status == 303
    # The phone is logged out right away; the laptop and the first session aren't.
    assert (await phone.get("/api/me")).status == 401
    assert (await laptop.get("/api/me")).status == 200 and (await olga.get("/api/me")).status == 200
    # Someone else's session id does nothing.
    jane_page = await client.get("/settings")
    jane_id = re.search(r'name="id" value="([0-9a-f]{16})"', jane_page.text).group(1)
    await laptop.post("/settings/sessions/revoke", form={"_csrf": csrf, "id": jane_id})
    assert (await client.get("/api/me")).status == 200
    # Log out other devices keeps this one.
    r = await laptop.post("/settings/sessions/revoke", form={"_csrf": csrf, "all_others": "1"})
    assert r.status == 303
    assert (await olga.get("/api/me")).status == 401 and (await laptop.get("/api/me")).status == 200


async def test_rename_delete_restore_and_empty_trash(app, client):
    pid = await new_project(client)
    assert (await client.request("PUT", f"/api/projects/{pid}/assets/fig.png", body=b"p" * 3_000)).status == 201
    csrf = await csrf_of(client, f"/p/{pid}")
    page = await client.get(f"/p/{pid}")
    assert 'value="rename-file"' in page.text and 'value="delete-file"' in page.text
    # Rename a document (the extension is kept when left out) and a file.
    r = await client.post(f"/p/{pid}", form={"_csrf": csrf, "action": "rename-file", "path": "paper.md",
                                             "new_path": "essay"})
    assert r.status == 303 and "error" not in r.header("location")
    tree = {e["path"] for e in (await client.get(f"/api/projects/{pid}/tree")).json()["tree"]}
    assert "essay.md" in tree and "paper.md" not in tree
    assert (app.rooms.project_dir(pid) / "essay.md").is_file()
    r = await client.request("PATCH", f"/api/projects/{pid}/assets/fig.png", json={"path": "figures/fig.png"})
    assert r.status == 200 and r.json()["path"] == "figures/fig.png"
    assert (app.rooms.project_dir(pid) / "figures" / "fig.png").read_bytes() == b"p" * 3_000
    assert not (app.rooms.project_dir(pid) / "fig.png").exists()
    # Delete both; they show in the trash and come back.
    for path in ("essay.md", "figures/fig.png"):
        r = await client.post(f"/p/{pid}", form={"_csrf": csrf, "action": "delete-file", "path": path})
        assert r.status == 303
    trash = (await client.get(f"/api/projects/{pid}/trash")).json()["trash"]
    assert {t["path"] for t in trash} == {"essay.md", "figures/fig.png"}
    page = await client.get(f"/p/{pid}")
    assert 'id="trash"' in page.text and "Empty trash permanently" in page.text
    doc = next(t for t in trash if t["type"] == "document")
    r = await client.post(f"/p/{pid}", form={"_csrf": csrf, "action": "restore-file", "doc_id": doc["id"]})
    assert r.status == 303
    assert (app.rooms.project_dir(pid) / "essay.md").is_file()
    assert (await open_editor(client, pid, "essay.md"))["text"].startswith("---")

    # Over quota, emptying the trash frees the space right away.
    row = await app.db.fetchone("SELECT id FROM users WHERE username = 'jane'")
    used = (await app.quotas.usage(row["id"]))["used"]
    await app.db.execute("UPDATE users SET quota_bytes = ? WHERE id = ?", (used + 1_000, row["id"]))
    r = await client.request("PUT", f"/api/projects/{pid}/assets/again.png", body=b"a" * 2_000)
    assert r.status == 413
    r = await client.post(f"/p/{pid}", form={"_csrf": csrf, "action": "empty-trash"})
    assert r.status == 303
    assert (await client.get(f"/api/projects/{pid}/trash")).json()["trash"] == []
    r = await client.request("PUT", f"/api/projects/{pid}/assets/again.png", body=b"a" * 2_000)
    assert r.status == 201, r.text


async def test_only_owners_empty_the_trash(app, client):
    pid = await new_project(client)
    olga = await make_user(app, "olga")
    await client.post(f"/api/projects/{pid}/members", json={"username": "olga", "role": "editor"})
    olga.csrf = (await olga.get("/api/me")).json()["csrf"]
    assert (await olga.post(f"/api/projects/{pid}/trash/empty", json={})).status == 403
    assert (await client.post(f"/api/projects/{pid}/trash/empty", json={})).status == 200


async def test_build_inputs_form(app, client):
    pid = await new_project(client)
    csrf = await csrf_of(client, f"/p/{pid}")
    r = await client.post(f"/p/{pid}", form={"_csrf": csrf, "action": "build-inputs",
                                             "inputs": "chapters/01.md\nchapters/02.md\n"})
    assert r.status == 303
    assert (await client.get(f"/api/projects/{pid}")).json()["buildInputs"] == ["chapters/01.md", "chapters/02.md"]
    assert "chapters/01.md\nchapters/02.md" in (await client.get(f"/p/{pid}")).text
    await client.post(f"/p/{pid}", form={"_csrf": csrf, "action": "build-inputs", "inputs": ""})
    assert (await client.get(f"/api/projects/{pid}")).json()["buildInputs"] is None


async def test_trashed_projects_in_settings(app, client):
    pid = await new_project(client, "Old draft")
    await client.request("DELETE", f"/api/projects/{pid}")
    page = await client.get("/settings")
    assert "Old draft" in page.text and "Deleted projects" in page.text
    csrf = await csrf_of(client)
    await client.post("/settings/trash", form={"_csrf": csrf, "action": "restore-project", "project_id": pid})
    assert (await client.get(f"/api/projects/{pid}")).status == 200
    await client.request("DELETE", f"/api/projects/{pid}")
    await client.post("/settings/trash", form={"_csrf": csrf, "action": "empty-all"})
    assert await app.db.fetchone("SELECT 1 FROM projects WHERE id = ?", (pid,)) is None
    assert not app.rooms.project_dir(pid).exists()


async def test_export_my_data(app, client):
    pid = await new_project(client, "Thesis")
    await client.request("PUT", f"/api/projects/{pid}/assets/fig.png", body=b"png")
    doc = (await open_editor(client, pid))["doc"]["id"]
    await client.post(f"/api/documents/{doc}/versions", json={"label": "First draft"})
    olga = await make_user(app, "olga")
    olga.csrf = (await olga.get("/api/me")).json()["csrf"]
    shared = (await olga.post("/api/projects", json={"name": "Olga's"})).json()["id"]
    await olga.post(f"/api/projects/{shared}/members", json={"username": "jane", "role": "viewer"})
    r = await client.get("/settings/export")
    assert r.status == 200 and r.header("content-type") == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.body))
    names = set(zf.namelist())
    assert "account.json" in names and "projects/Thesis/paper.md" in names and "projects/Thesis/fig.png" in names
    assert "shared/Olgas/paper.md" in names
    assert any(n.startswith("versions/Thesis/paper.md.") and "First draft" in n for n in names)
    meta = json.loads(zf.read("account.json"))
    assert meta["account"]["username"] == "jane" and {p["role"] for p in meta["projects"]} == {"owner", "viewer"}
    assert "password_hash" not in json.dumps(meta) and "totp" not in json.dumps(meta)


async def test_delete_account(app, client):
    olga = await make_user(app, "olga")
    olga.csrf = (await olga.get("/api/me")).json()["csrf"]
    solo = (await olga.post("/api/projects", json={"name": "Solo"})).json()
    team = (await olga.post("/api/projects", json={"name": "Team"})).json()
    await make_user(app, "emil")
    await olga.post(f"/api/projects/{team['id']}/members", json={"username": "emil", "role": "editor"})
    # A published site of the solo project.
    await app.db.execute("INSERT INTO publications(project_id, doc_id, slug, visibility) VALUES (?, ?, 'olga-solo',"
                         " 'public')", (solo["id"], solo["mainDocument"]))
    site = app.publisher.root / "olga-solo"
    site.mkdir(parents=True)
    (site / "index.html").write_text("hi")
    uid = (await app.db.fetchone("SELECT id FROM users WHERE username = 'olga'"))[0]
    await app.db.execute("INSERT INTO identities(user_id, provider, issuer, subject, email) VALUES (?, 'kc',"
                         " 'https://id.example', 'sub-1', 'olga@example.org')", (uid,))
    await app.db.execute("INSERT INTO audit_log(actor, action, target, meta_json) VALUES ('user:1', 'user.update',"
                         " ?, '{\"username\": \"olga\"}')", (f"user:{uid}",))

    csrf = await csrf_of(olga)
    r = await olga.post("/settings/delete-account", form={"_csrf": csrf, "confirm_username": "olga",
                                                          "password": "wrong", "hand_over": "1"})
    assert r.status == 400 and "current password is wrong" in r.text
    r = await olga.post("/settings/delete-account", form={"_csrf": csrf, "confirm_username": "nope",
                                                          "password": USER_PASSWORD})
    assert r.status == 400
    r = await olga.post("/settings/delete-account", form={"_csrf": csrf, "confirm_username": "olga",
                                                          "password": USER_PASSWORD, "hand_over": "1"})
    assert r.status == 303 and r.header("location") == "/login?deleted=1"
    assert (await olga.get("/api/me")).status == 401
    for sql in ("SELECT 1 FROM users WHERE id = ?", "SELECT 1 FROM sessions WHERE user_id = ?",
                "SELECT 1 FROM identities WHERE user_id = ?", "SELECT 1 FROM project_members WHERE user_id = ?"):
        assert await app.db.fetchone(sql, (uid,)) is None, sql
    # The solo project and its site are gone; the team project went to Emil.
    assert await app.db.fetchone("SELECT 1 FROM projects WHERE id = ?", (solo["id"],)) is None
    assert await app.db.fetchone("SELECT 1 FROM publications WHERE slug = 'olga-solo'") is None
    assert not site.exists()
    emil = (await app.db.fetchone("SELECT id FROM users WHERE username = 'emil'"))[0]
    row = await app.db.fetchone("SELECT owner_id FROM projects WHERE id = ?", (team["id"],))
    assert row["owner_id"] == emil
    assert (await app.db.fetchone("SELECT role FROM project_members WHERE project_id = ? AND user_id = ?",
                                  (team["id"], emil)))[0] == "owner"
    # The audit log keeps that it happened, without the details.
    rows = await app.db.fetchall("SELECT action, meta_json FROM audit_log WHERE target = ?", (f"user:{uid}",))
    assert "account.delete" in {r["action"] for r in rows}
    assert all("olga" not in r["meta_json"] for r in rows)


async def test_the_last_admin_cannot_delete_themselves(app, client):
    csrf = await csrf_of(client)
    r = await client.post("/settings/delete-account", form={"_csrf": csrf, "confirm_username": "jane",
                                                            "password": "correct horse battery"})
    assert r.status == 400 and "only admin" in r.text
    assert (await client.get("/api/me")).status == 200
