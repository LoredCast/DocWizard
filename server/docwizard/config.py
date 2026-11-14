"""Configuration: docwizard.toml (tomllib) plus environment overrides.

Every key can be overridden with ``DOCWIZARD_<SECTION>_<KEY>``. Keys ending in
``_file`` name a file whose contents become the value of the key without the
suffix (``password_file`` -> ``password``).
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass
class ServerConfig:
    public_url: str = "http://localhost:8080"
    publish_url: str = "http://127.0.0.1:8080"
    preview_url: str = "http://127.0.0.1:8080/_preview"
    socket: str = ""  # Unix socket; empty means listen on host:port
    host: str = "127.0.0.1"
    port: int = 8080
    data_dir: str = "./data/app"
    publish_dir: str = "./data/publish"
    jobs_dir: str = "./data/jobs"
    cache_dir: str = "./data/cache"
    durability: str = "full"
    serve_static: bool = True
    source_url: str = "https://github.com/LoredCast/DocWizard"
    release: str = ""


@dataclass
class SecurityConfig:
    secret_key_file: str = ""
    allow_private_outbound: list[str] = field(default_factory=list)
    session_idle_days: int = 14
    guest_session_hours: int = 24


@dataclass
class AccountsConfig:
    registration: str = "closed"  # open | invite | oidc-only | closed
    guests: bool = True
    guest_max_role: str = "editor"
    scratch_projects: bool = False
    require_totp_for_admins: bool = False
    min_password_length: int = 12


@dataclass
class RenderConfig:
    worker_socket: str = ""  # default: <jobs_dir>/worker.sock
    spawn_worker: bool = False  # development: start the worker as a child process
    concurrency: int = 2
    timeout_seconds: int = 90
    memory_mb: int = 1536
    preview_debounce_ms: int = 300
    default_layout: str = "default"


@dataclass
class LimitsConfig:
    max_document_bytes: int = 5_000_000
    max_upload_bytes: int = 50_000_000
    default_quota_bytes: int = 2_000_000_000


@dataclass
class CollabConfig:
    op_retention_days: int = 7
    snapshot_every: int = 500
    max_push_churn: int = 1_000_000
    materialize_delay: float = 2.0
    materialize_max_delay: float = 30.0
    snapshot_idle_seconds: float = 60.0
    disk_poll_seconds: float = 5.0
    room_idle_seconds: float = 600.0


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    accounts: AccountsConfig = field(default_factory=AccountsConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    collab: CollabConfig = field(default_factory=CollabConfig)

    # -- derived values -------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return Path(self.server.data_dir)

    @property
    def jobs_dir(self) -> Path:
        return Path(self.server.jobs_dir)

    @property
    def cache_dir(self) -> Path:
        return Path(self.server.cache_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "docwizard.db"

    @property
    def worker_socket(self) -> Path:
        return Path(self.render.worker_socket or self.jobs_dir / "worker.sock")

    @property
    def app_origin(self) -> str:
        return origin_of(self.server.public_url)

    @property
    def preview_origin(self) -> str:
        return origin_of(self.server.preview_url)

    @property
    def secure_cookies(self) -> bool:
        return urlsplit(self.server.public_url).scheme == "https"

    @property
    def cookie_name(self) -> str:
        # The __Host- prefix requires Secure, which needs HTTPS.
        return "__Host-docwizard" if self.secure_cookies else "docwizard"

    @property
    def release(self) -> str:
        from . import __version__

        return self.server.release or __version__


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


_SECTIONS = {f.name: f.type for f in dataclasses.fields(Config)}


def _coerce(value: str, current: Any) -> Any:
    if isinstance(current, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(value.replace("_", ""))
    if isinstance(current, float):
        return float(value)
    if isinstance(current, list):
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def _apply_section(obj: Any, values: dict[str, Any], section: str) -> None:
    names = {f.name for f in dataclasses.fields(obj)}
    for key, value in values.items():
        if key.endswith("_file") and key[:-5] in names and key not in names:
            value = Path(value).read_text().strip()
            key = key[:-5]
        if key not in names:
            raise ValueError(f"unknown config key [{section}] {key}")
        setattr(obj, key, value)


def load(path: str | os.PathLike | None = None, env: dict[str, str] | None = None) -> Config:
    """Load configuration from *path* (if it exists) and the environment."""
    env = dict(os.environ if env is None else env)
    cfg = Config()
    if path is None:
        path = env.get("DOCWIZARD_CONFIG")
        if path is None:
            for candidate in ("docwizard.toml", "/etc/docwizard/docwizard.toml"):
                if Path(candidate).is_file():
                    path = candidate
                    break
    if path is not None and Path(path).is_file():
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        for section, values in data.items():
            if section not in _SECTIONS or not isinstance(values, dict):
                continue  # [[oidc]] and [smtp] are read by later milestones
            _apply_section(getattr(cfg, section), values, section)

    for name, value in env.items():
        if not name.startswith("DOCWIZARD_") or name == "DOCWIZARD_CONFIG":
            continue
        rest = name[len("DOCWIZARD_"):].lower()
        section, _, key = rest.partition("_")
        if section not in _SECTIONS or not key:
            continue
        obj = getattr(cfg, section)
        if key.endswith("_file") and hasattr(obj, key[:-5]) and not hasattr(obj, key):
            value = Path(value).read_text().strip()
            key = key[:-5]
        if not hasattr(obj, key):
            continue
        setattr(obj, key, _coerce(value, getattr(obj, key)))
    if cfg.server.durability not in ("full", "normal"):
        raise ValueError("server.durability must be 'full' or 'normal'")
    return cfg
