# DocWizard

[![CI & Container Releases](https://github.com/LoredCast/DocWizard/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/LoredCast/DocWizard/actions/workflows/ci-cd.yml)
[![Version](https://img.shields.io/github/v/tag/LoredCast/DocWizard?label=version&sort=semver)](https://github.com/LoredCast/DocWizard/tags)
[![ghcr.io Pulls](https://img.shields.io/badge/dynamic/json?logo=github&url=https%3A%2F%2Fipitio.github.io%2Fbackage%2FLoredCast%2FDocWizard%2Fdocwizard-app.json&query=%24.downloads&label=ghcr.io%20pulls&cacheSeconds=14400)](https://github.com/LoredCast/DocWizard/pkgs/container/docwizard-app)

DocWizard is a self-hosted, collaborative editor for structured writing: term papers, theses, articles, and reports. You write plain-text Pandoc Markdown (or LaTeX) in the browser, while **layouts** control all styling and exports through Pandoc, Typst, LaTeX, and Citeproc.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design reference.

## Features

- **Writing & Crash Safety**: CodeMirror 6 editor with Pandoc Markdown support (front matter, citations, math rendered with Temml, footnotes, attributes, fenced divs), formatting toolbar, live syntax preview, heading folding, word count, LaTeX mode, named versions, and SQLite FTS5 full-text search. Every edit is committed to SQLite (`synchronous=FULL`) and materialized to plain files on disk; external edits are reconciled with visible conflict markers instead of silent overwrites.
- **Collaboration & Offline**: Real-time multi-user editing via OT (`@codemirror/collab`) with live cursors and presence, anchored comment threads, role-based share links (`owner`, `editor`, `commenter`, `viewer`), optional guest access, and offline editing via service worker + IndexedDB.
- **Previews, Layouts & Citations**: Sandboxed HTML previews and PDF previews (native viewer on desktop, lazy-loaded PDF.js on mobile/unsupported browsers). Ships `default`, `term-paper`, and `article` layouts, seven CSL styles (Chicago author-date/notes, APA, MLA, Harvard, IEEE, DIN 1505-2), Zotero/BibTeX/RIS/CSL-JSON citation autocomplete, multi-file LaTeX builds (`latexmk`), static site publishing, and exports to PDF (Typst or LaTeX), DOCX, ODT, HTML, Markdown, Typst/LaTeX source, and ZIP.
- **Sync, Auth & Administration**: Two-way WebDAV sync (Nextcloud, Seafile, generic WebDAV), local accounts with TOTP two-step login, OpenID Connect (`[[oidc]]`), AES-256-GCM encrypted credentials, `/admin` dashboard with Prometheus `/metrics`, and English/German UI.

*Not yet implemented:* SyncTeX jump-to-source in PDFs and EPUB export.

## Quick Start (Docker)

The recommended deployment runs three containers (`app`, `worker`, and `proxy`/Caddy) using prebuilt multi-arch images (`linux/amd64`, `linux/arm64`) from GHCR.

1. Download the Compose file and example configuration:

   ```sh
   curl -fsSLO https://raw.githubusercontent.com/LoredCast/DocWizard/main/deploy/compose.yaml
   curl -fsSL https://raw.githubusercontent.com/LoredCast/DocWizard/main/deploy/docwizard.example.toml -o docwizard.toml
   ```

2. Edit `docwizard.toml` with your URLs (for example `public_url = "https://write.example.org"`, and optionally a separate `publish_url` / `preview_url`).

3. Start the stack and create an admin account:

   ```sh
   docker compose up -d
   docker compose exec app python -m docwizard create-admin you
   ```

By default, Caddy listens on `127.0.0.1:8080` (app) and `127.0.0.1:8081` (publish/preview) behind your reverse proxy (forward `Host`, `X-Forwarded-Proto`, and WebSockets).

### Deployment Options

- **Release channels (`DOCWIZARD_TAG`)**:
  - `:main` (default): rolling builds from `main` (and `:sha-<commit>`).
  - `:nightly`: pre-release builds from `nightly` (also `:nightly-YYYYMMDD`). Enable with `DOCWIZARD_TAG=nightly` or `-f deploy/compose.nightly.yaml`.
  - `:latest` or `:<version>` (e.g. `0.1.0`): tagged releases.
- **Built-in TLS**: Let Caddy manage HTTPS on ports `80` and `443` with [deploy/compose.ssl.yaml](deploy/compose.ssl.yaml):

  ```sh
  DOCWIZARD_APP_HOST=write.example.org DOCWIZARD_PUBLISH_HOST=pub.example.org \
    docker compose -f deploy/compose.yaml -f deploy/compose.ssl.yaml up -d
  ```

- **Local checkout & smaller worker**: Run `docker compose -f deploy/compose.yaml up -d --build` from the repo (use `--build-arg WITH_LATEX=0` to omit TeX Live from the worker image).
- **Bare metal**: systemd units are provided in [deploy/systemd/](deploy/systemd).

## Development

Prerequisites: Python 3.12+, [Pandoc](https://pandoc.org/installing.html) 3.9+, [Typst](https://github.com/typst/typst/releases) 0.14+, and optionally TeX Live (`latexmk` and LuaLaTeX) for LaTeX projects.

```sh
python -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install -e '.[dev]'

python -m docwizard create-admin you
DOCWIZARD_RENDER_SPAWN_WORKER=true python -m docwizard serve
```

Open <http://localhost:8080>. By default in local dev, HTML previews are served from `http://127.0.0.1:8080/_preview` as a separate origin, so open the app via `localhost` (or set `DOCWIZARD_SERVER_PUBLIC_URL` to your LAN/tunnel URL for same-origin sandboxed previews). Data is stored in `./data/`.

Run `python -m docwizard check` to verify tools and directories.

## Configuration & Operations

Settings are read from `docwizard.toml` (see [deploy/docwizard.example.toml](deploy/docwizard.example.toml)) and can be overridden with `DOCWIZARD_<SECTION>_<KEY>` environment variables.

- **Mail (`[smtp]`)**: Enables password-reset and invitation emails. Without it, admins copy one-time links directly from `/admin/users`.
- **OIDC (`[[oidc]]`)**: Register `<public_url>/login/oidc/<id>/callback` as the redirect URI at your provider.
- **Secret key**: Created automatically at `<data_dir>/secret.key` (or set `security.secret_key_file`). Back it up separately from your database; rotate keys with `python -m docwizard rotate-key`.
- **Private LAN sync hosts**: List internal WebDAV/Nextcloud hosts in `security.allow_private_outbound` (or in `/admin/settings`).
- **Backups**: Back up and restore consistent snapshots with:

  ```sh
  python -m docwizard backup <dir>
  python -m docwizard restore <dir>
  ```

## Tests

```sh
pytest
```

The suite covers unit, property, adversarial collaboration protocol, and integration tests. Pandoc, Typst, LaTeX, and CodeMirror/frontend cross-check tests run automatically when `pandoc`, `typst`, `latexmk`, and `node` are available (set `DOCWIZARD_TEST_TOOLS_PATH` to point at specific `pandoc`/`typst` binaries).

## Repository

| Path | Purpose |
|---|---|
| `server/docwizard/` | ASGI app: HTTP, auth, projects, collaboration, rendering, citations, sync, admin |
| `server/docwizard/locale/` | Interface translations (`de.json`) |
| `worker/` | Isolated render worker (Python stdlib only) |
| `web/` | Stylesheet, ES modules, service worker, and vendored CodeMirror / Temml / PDF.js |
| `layouts/` | Built-in layouts (`default`, `term-paper`, `article`) |
| `csl/` | Bundled CSL citation styles |
| `filters/` | Built-in Lua filters (`crossref`, `pagebreak`, `wordcount`, `zotero`) |
| `deploy/` | Dockerfiles, Compose files, Caddyfiles, and systemd units |
| `scripts/vendor.sh` | Rebuilds and checksums vendored frontend bundles |

## License

GNU Affero General Public License v3.0 or later — see [LICENSE](LICENSE).
