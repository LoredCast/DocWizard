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
    # Search engines may index public publications (§14.4). Unlisted ones are never indexed.
    index_public_sites: bool = True


@dataclass
class LegalConfig:
    """Links shown in the footer of the login, registration, share and error pages (§13.2)."""
    terms_url: str = ""
    privacy_url: str = ""
    imprint_url: str = ""
    abuse_email: str = ""


@dataclass
class SecurityConfig:
    secret_key_file: str = ""
    allow_private_outbound: list[str] = field(default_factory=list)
    session_idle_days: int = 14
    guest_session_hours: int = 24
    # An optional anti-bot challenge on registration and password reset (§13.2): "turnstile"
    # (Cloudflare) or "hcaptcha", checked on the server with captcha_secret. Empty: off.
    captcha_provider: str = ""
    captcha_site_key: str = ""
    captcha_secret: str = ""


@dataclass
class AccountsConfig:
    registration: str = "closed"  # open | invite | oidc-only | closed (invite: admins invite people)
    guests: bool = True
    guest_max_role: str = "editor"
    require_totp_for_admins: bool = False
    min_password_length: int = 12
    # Registration asks for an email address, and publishing, sync and Zotero wait until it is
    # confirmed through a mailed link (§13.2). Needs [smtp].
    require_email_verification: bool = False


@dataclass
class RenderConfig:
    worker_socket: str = ""  # default: <jobs_dir>/worker.sock
    spawn_worker: bool = False  # development: start the worker as a child process
    concurrency: int = 2  # jobs at once; give the worker the same number (DOCWIZARD_WORKER_CONCURRENCY)
    timeout_seconds: int = 90  # users.render_timeout_seconds overrides it for the projects someone owns
    memory_mb: int = 1536
    preview_debounce_ms: int = 300
    default_layout: str = "default"
    # Fairness (§9.2). Slots kept for quick targets (HTML, Typst PDF, Word...), so LaTeX builds can't
    # take them all; at most concurrency - 1.
    fast_lane: int = 1
    user_previews: int = 1  # previews one person renders at once; more wait their turn
    user_exports: int = 2  # exports one person may have queued or running; more get 429
    per_user_rpm: int = 0  # renders (cache misses) per person per minute; 0: no limit
    latex_daily_limit: int = 0  # LaTeX builds per person per day; 0: no limit
    # LaTeX for projects whose owner has no users.allow_latex setting of their own.
    allow_latex: bool = True


@dataclass
class LimitsConfig:
    max_document_bytes: int = 5_000_000
    max_upload_bytes: int = 50_000_000
    # Storage per account (§7.7): what the projects someone owns take up, trash and named versions
    # included. users.quota_bytes overrides it per account. 0 or less: no limit.
    default_quota_bytes: int = 2_000_000_000
    # 0: no limit. Generous or off for a private server, tightened for public hosting.
    max_projects_per_user: int = 0
    max_files_per_project: int = 0


@dataclass
class SMTPConfig:
    host: str = ""  # empty: no mail; admins hand out reset and invitation links themselves
    port: int = 587
    starttls: bool = True
    tls: bool = False  # implicit TLS, usually port 465
    username: str = ""
    password: str = ""
    sender: str = ""  # "from" in the config file


@dataclass
class SyncConfig:
    enabled: bool = True  # tests turn the background poller off and run syncs directly
    default_poll_seconds: int = 60
    max_files_per_target: int = 5000
    # Folders on this server that projects may sync with (§12.6), for example one that Syncthing,
    # Dropbox or a mounted share also uses. Empty: folder sync is off.
    folder_roots: list[str] = field(default_factory=list)
    # Git repositories over HTTPS (§12.7). Needs the git command on the app's PATH.
    git: bool = True


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
class GrammarConfig:
    """Grammar checking in the editor (§6.8)."""
    harper: bool = True  # Harper runs in the browser, when its files are installed (deploy/fetch_tools.py)
    languagetool_url: str = ""  # a LanguageTool server, e.g. http://languagetool:8010; empty: none
    languagetool_api_key: str = ""
    languagetool_username: str = ""


@dataclass
class OIDCProvider:
    """One [[oidc]] entry (§13.4)."""
    id: str = ""
    label: str = ""
    issuer: str = ""
    client_id: str = ""
    client_secret: str = ""
    auth_method: str = "client_secret_basic"  # or client_secret_post
    scopes: list[str] = field(default_factory=lambda: ["openid", "profile", "email"])
    username_claim: str = "preferred_username"
    groups_claim: str = "groups"
    allowed_groups: list[str] = field(default_factory=list)
    admin_groups: list[str] = field(default_factory=list)
    auto_create: bool = True


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    accounts: AccountsConfig = field(default_factory=AccountsConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    collab: CollabConfig = field(default_factory=CollabConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    smtp: SMTPConfig = field(default_factory=SMTPConfig)
    grammar: GrammarConfig = field(default_factory=GrammarConfig)
    legal: LegalConfig = field(default_factory=LegalConfig)
    oidc: list[OIDCProvider] = field(default_factory=list)

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


def _is_loopback_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in ("", "localhost", "127.0.0.1", "::1")


_SECTIONS = {f.name: f.type for f in dataclasses.fields(Config)}

# Keys that older releases knew and that are now ignored, so existing config files keep working.
DEPRECATED_KEYS = {("accounts", "scratch_projects")}
CAPTCHA_PROVIDERS = ("", "turnstile", "hcaptcha")


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
        if (section, key) in DEPRECATED_KEYS:
            import logging

            logging.getLogger("docwizard").warning("config key [%s] %s is no longer used; remove it", section, key)
            continue
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
            if section == "oidc" and isinstance(values, list):
                for entry in values:
                    provider = OIDCProvider()
                    _apply_section(provider, entry, "oidc")
                    if not (provider.id and provider.issuer and provider.client_id):
                        raise ValueError("each [[oidc]] entry needs id, issuer and client_id")
                    provider.label = provider.label or provider.id
                    cfg.oidc.append(provider)
                continue
            if section not in _SECTIONS or not isinstance(values, dict):
                continue
            if section == "smtp" and "from" in values:
                values = {**values, "sender": values["from"]}
                del values["from"]
            _apply_section(getattr(cfg, section), values, section)

    for name, value in env.items():
        if not name.startswith("DOCWIZARD_") or name == "DOCWIZARD_CONFIG":
            continue
        rest = name[len("DOCWIZARD_"):].lower()
        section, _, key = rest.partition("_")
        if section not in _SECTIONS or not key or section == "oidc":
            continue
        if section == "smtp" and key == "from":
            key = "sender"
        obj = getattr(cfg, section)
        if key.endswith("_file") and hasattr(obj, key[:-5]) and not hasattr(obj, key):
            value = Path(value).read_text().strip()
            key = key[:-5]
        if not hasattr(obj, key):
            continue
        setattr(obj, key, _coerce(value, getattr(obj, key)))
    if cfg.accounts.registration not in ("open", "invite", "oidc-only", "closed"):
        raise ValueError("accounts.registration must be open, invite, oidc-only or closed")
    if cfg.security.captcha_provider not in CAPTCHA_PROVIDERS:
        raise ValueError("security.captcha_provider must be turnstile, hcaptcha or empty")
    if cfg.security.captcha_provider and not (cfg.security.captcha_site_key and cfg.security.captcha_secret):
        raise ValueError("security.captcha_provider needs captcha_site_key and captcha_secret")
    if cfg.accounts.require_email_verification and not cfg.smtp.host:
        raise ValueError("accounts.require_email_verification needs an [smtp] section to send the links")
    for key in ("terms_url", "privacy_url", "imprint_url"):
        value = getattr(cfg.legal, key)
        if value and urlsplit(value).scheme not in ("http", "https"):
            raise ValueError(f"legal.{key} must be an http or https URL")
    if cfg.legal.abuse_email and "@" not in cfg.legal.abuse_email:
        raise ValueError("legal.abuse_email must be an email address")
    if cfg.server.durability not in ("full", "normal"):
        raise ValueError("server.durability must be 'full' or 'normal'")
    if not _is_loopback_url(cfg.server.public_url) and _is_loopback_url(cfg.server.preview_url):
        cfg.server.preview_url = f"{cfg.app_origin}/_preview"
    return cfg
