"""Command line: serve, migrate, create-admin, reset-password, backup, restore, check."""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import sys

from . import config as config_mod
from . import logs


def _db(cfg):
    from .db.database import Database

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.db_path, cfg.server.durability)
    db.open()
    return db


def _read_password(args, username: str, min_length: int) -> str:
    from .auth import passwords

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass(f"Password for {username}: ")
        if getpass.getpass("Repeat: ") != password:
            sys.exit("The passwords don't match.")
    problem = passwords.check_policy(password, min_length, username)
    if problem:
        sys.exit(problem)
    return password


def cmd_serve(cfg, args) -> None:
    import uvicorn

    from .asgi import create_app

    logs.setup(os.environ.get("DOCWIZARD_LOG_LEVEL", "INFO"))
    app = create_app(cfg)
    kwargs = dict(ws="wsproto", lifespan="on", log_config=None, proxy_headers=False,
                  server_header=False, date_header=False, ws_max_size=1 << 20)
    if cfg.server.socket:
        uvicorn.run(app, uds=cfg.server.socket, **kwargs)
    else:
        uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, **kwargs)


def cmd_migrate(cfg, args) -> None:
    from .db import migrate

    db = _db(cfg)
    print(f"Database is at migration {db.read_sync(migrate.current_version)}.")
    db.close()


def cmd_create_admin(cfg, args) -> None:
    from .auth import passwords, users

    password = _read_password(args, args.username, cfg.accounts.min_password_length)
    pw_hash = passwords.hash_password(password)
    db = _db(cfg)
    try:
        db.write_sync(lambda c: users.create(c, args.username, pw_hash, display_name=args.display_name or "",
                                             email=args.email, role="admin"))
    except ValueError as exc:
        sys.exit(str(exc))
    finally:
        db.close()
    print(f"Created admin {args.username}.")


def cmd_reset_password(cfg, args) -> None:
    from .auth import passwords, sessions, users

    db = _db(cfg)
    try:
        row = db.read_sync(lambda c: users.get_by_username(c, args.username))
        if row is None:
            sys.exit(f"No user named {args.username}.")
        password = _read_password(args, args.username, cfg.accounts.min_password_length)
        pw_hash = passwords.hash_password(password)

        def work(c):
            users.set_password(c, row["id"], pw_hash)
            sessions.delete_for_user(c, row["id"])

        db.write_sync(work)
    finally:
        db.close()
    print(f"Password for {args.username} changed; their sessions were ended.")


def cmd_backup(cfg, args) -> None:
    from .projects import backup

    backup.backup(cfg, args.dest)
    print(f"Backup written to {args.dest}.")


def cmd_restore(cfg, args) -> None:
    from .projects import backup

    backup.restore(cfg, args.src)
    print("Restored. Project folders were rebuilt from the database.")


def cmd_check(cfg, args) -> None:
    problems = 0
    for tool in ("pandoc", "typst"):
        path = shutil.which(tool)
        print(f"{tool:8} {'found at ' + path if path else 'not found (needed by the render worker)'}")
    for name, path in (("data", cfg.data_dir), ("jobs", cfg.jobs_dir), ("cache", cfg.cache_dir)):
        ok = path.exists() and os.access(path, os.W_OK)
        print(f"{name:8} {path} {'writable' if ok else 'missing or not writable'}")
        problems += not ok
    sock = cfg.worker_socket
    print(f"worker   {sock} {'present' if sock.exists() else 'not running'}")
    sys.exit(1 if problems else 0)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="docwizard")
    parser.add_argument("--config", help="path to docwizard.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the app server")
    sub.add_parser("migrate", help="apply database migrations")
    p = sub.add_parser("create-admin", help="create an admin account")
    p.add_argument("username")
    p.add_argument("--display-name")
    p.add_argument("--email")
    p.add_argument("--password-stdin", action="store_true")
    p = sub.add_parser("reset-password", help="set a new password for a user")
    p.add_argument("username")
    p.add_argument("--password-stdin", action="store_true")
    p = sub.add_parser("backup", help="back up the database, blobs and layouts")
    p.add_argument("dest")
    p = sub.add_parser("restore", help="restore a backup and rebuild project folders")
    p.add_argument("src")
    sub.add_parser("check", help="check tools and directories")
    args = parser.parse_args(argv)
    cfg = config_mod.load(args.config)
    handlers = {
        "serve": cmd_serve, "migrate": cmd_migrate, "create-admin": cmd_create_admin,
        "reset-password": cmd_reset_password, "backup": cmd_backup, "restore": cmd_restore, "check": cmd_check,
    }
    handlers[args.command](cfg, args)


if __name__ == "__main__":
    main()
