# DocWizard

[![CI & Container Releases](https://github.com/LoredCast/DocWizard/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/LoredCast/DocWizard/actions/workflows/ci-cd.yml)
[![Version](https://img.shields.io/github/v/tag/LoredCast/DocWizard?label=version&sort=semver)](https://github.com/LoredCast/DocWizard/tags)
[![ghcr.io Pulls](https://img.shields.io/badge/dynamic/json?logo=github&url=https%3A%2F%2Fipitio.github.io%2Fbackage%2FLoredCast%2FDocWizard%2Fdocwizard-app.json&query=%24.downloads&label=ghcr.io%20pulls&cacheSeconds=14400)](https://github.com/LoredCast/DocWizard/pkgs/container/docwizard-app)

A self-hostable editor for structured writing: term papers, theses, articles and reports. You write pandoc Markdown in the browser; **layouts** decide how the output looks. Exports go through pandoc, with Typst for PDF and citeproc for citations.

[ARCHITECTURE.md](ARCHITECTURE.md) is the design reference. This version covers the roadmap milestones M0 to M6 (§21): reliable editing, submission-ready output, collaboration, sharing and comments, offline work, the integrations, and LaTeX with the rest of the polish.

## What works

### Writing

- **Editing**: CodeMirror with pandoc Markdown (front matter, citations, math, footnotes, attributes, fenced divs), live preview of headings, emphasis, links, task lists, citation chips and MathML, citation autocomplete, heading folding and a word count. A LaTeX mode for `.tex` files.
- **Saving that survives crashes**: every edit is committed to SQLite (`synchronous=FULL`) before it is acknowledged, then written to plain files in the project folder. On startup, the files are reconstructed from the database. Edits made to those files outside DocWizard are detected and reconciled, with real conflicts kept visible instead of blended.
- **Versions and history**: named versions plus an automatic history (every autosave of the last hour, then hourly for two days, daily for two months and weekly after that), each with who edited. Compare any of them with the current text word by word, and restore (the current text is kept as a version first).
- **Search**: full-text search across every document you can open, with SQLite FTS5.

### Working together

- **Collaboration**: several tabs or people edit the same document (OT with a central authority, compatible with `@codemirror/collab`), with live cursors, selections and a list of who is here. The protocol is tested against injected failures.
- **Sharing**: project roles (owner, editor, commenter, viewer), share links with a role and an expiry, and guests who join through a link without an account (admins decide whether guests are allowed and their highest role).
- **Comments**: threads anchored to text that stay in place while people edit, with replies and resolving. Recent documents and pins on the home page.
- **Review links**: a share link can open documents in the review view, for supervisors, editors and producers who don't write Markdown: the text as it reads, set like a page, where selecting a passage offers Comment and Suggest change.
- **Suggestions**: select text and suggest new wording; the editor shows it struck through with the new text beside it, and editors accept it (it becomes an edit) or reject it. Commenters can suggest too, which makes review links useful without edit rights.
- **Mentions and notifications**: `@username` in a comment notifies that member; replies notify everyone who wrote in the thread. Notifications appear on the home page and in the editor, and unread ones are mailed (mentions and replies, only mentions, or never, per person).
- **Offline**: a service worker keeps the app and the documents this browser has stored available offline. Edits made offline are kept in IndexedDB and come back as a merge, with overlapping changes shown as conflicts in their own panel, never blended.

### Output

- **Rendering**: a separate worker runs pandoc, Typst and latexmk, each job as its own Unix user with resource limits. HTML preview on a separate preview origin through capability URLs; PDF preview; exports to PDF (Typst or LaTeX), DOCX, ODT, HTML, Markdown, Typst and LaTeX source, and a zip of the project.
- **Layouts**: `default`, `term-paper` (title page, numbered sections, 1.5 spacing), `article` (two columns with an abstract), and layouts after real style guides: `oxford` (University of Oxford Style Guide), `uni-wien-philosophie` (the Vienna philosophy department's *Leitfaden*, with title page, plagiarism declaration and its own citation styles), `aps` (Physical Review), `turabian` (Turabian student papers), `mla`, `apa-student` (APA student papers), `mhra`, `ieee`, and the German university guides `wuerzburg-germanistik` and `goettingen-vcm`; manuscripts for publishers: `manuscript-story` and `manuscript-novel` (Shunn's format), `normseite` (the German standard page) and `book-manuscript`; and scripts written in plain Markdown: `screenplay`, `drehbuch` and `av-script` (two-column documentary scripts). Projects pick options such as paper size, font size, spacing and citation style. The editor's Fields panel shows which front matter fields the layout prints (course, instructor, student ID, …), what each means and where it appears, and adds them in one click; typing in the front matter suggests them and hovering explains them. Layouts can extend each other. Lua filters in installed layouts only run after an admin approves them, by hash.
- **Layout editor**: make a copy of any layout and change how everything renders, with a live PDF preview: paper and margins, columns, page numbers and running header, fonts and sizes, line and paragraph spacing, indents, four heading levels (run-in headings too) (size, weight, case, alignment, spacing, numbering schemes), the title block or title page and which front matter fields it shows, a declaration page, the table of contents, block quotations, footnotes, citation style and bibliography, captions, and which options authors get. Custom layouts store only what differs from the layout they are based on, so they follow its updates, and can be shared with everyone on the server. Fonts for the PDF: Libertinus Serif, New Computer Modern, Noto Serif and Noto Sans, Tinos (Times New Roman metrics), Carlito (Calibri metrics), Cousine and DejaVu Sans Mono.
- **Citations**: from CSL-JSON, BibTeX or RIS files in the project, or from a Zotero library. Typing `@` completes keys from the document's bibliography first, then from the project's other files and your Zotero libraries, and adds the file or library to the front matter when needed. Bundled styles: Chicago author-date, notes and shortened notes (18th and 17th editions), APA, MLA, MHRA (notes and author-date), Harvard, IEEE, American Physical Society, Oxford Guide to Style, DIN 1505-2, the Universität Wien philosophy styles (footnotes and short citations) and Würzburg Germanistik short citations. Cross-references to figures, tables and equations.
- **Document languages**: a document or project declares its language (`lang: de-AT`) and marks passages in others (`[mot]{lang=fr}`). Dates, quotation marks, hyphenation, the bibliography's terms and every generated heading and label (Contents, Abstract, Figure, References or Literaturverzeichnis) follow it.
- **LaTeX projects**: `.tex` documents and multi-file projects build with latexmk (LuaLaTeX by default, no shell escape), with errors and warnings shown at the right line. Markdown documents can also go to PDF through LaTeX.
- **Publishing**: a document can be published as a static site on the publish origin, at a chosen or unlisted address, optionally with a PDF. It is updated a few seconds after edits and taken down when the document is deleted.

### Integrations

- **Sync**: projects sync both ways with Nextcloud, Seafile or any WebDAV server, with the same reconciliation and conflicts as outside edits.
- **Zotero**: add a personal or group library from zotero.org with an API key, or Zotero desktop's local API. A References panel in the editor searches your libraries and browses their collections; citing an item adds its library to the document's front matter. PDF attachments open from Zotero's file storage or from the WebDAV folder Zotero syncs to, and the page finder searches a PDF for the quoted passage and puts the printed page into the citation's locator.
- **Login providers**: any OpenID Connect provider, such as Keycloak or a university login, with allowed groups, admin groups and linking to existing accounts.
- Stored credentials (WebDAV passwords, Zotero keys, authenticator secrets) are encrypted with the server's secret key. `python -m docwizard rotate-key` adds a new key and re-encrypts them. Outbound requests to private networks are refused unless an admin lists the host.

### Accounts and administration

- **Accounts**: local accounts, sessions, CSRF protection, two-step login with an authenticator app and recovery codes, password reset by mail, and invitations.
- **Admin area** at `/admin`: health (worker, tool versions, disk, recent errors), users (invite, disable, reset links, require two-step login, quotas), settings that override the configuration file (registration, guests, password length, default layout, allowed private hosts), layout filter approval, and an audit log. Prometheus metrics at `/metrics` for admins.
- **Maintenance**: a daily job empties trash older than 30 days, removes expired links and deletes unreferenced files. Backup and restore from the command line.
- **Languages**: English and German. The language follows the browser until you choose one in your settings.

Not yet: PDF.js with SyncTeX (jumping between the PDF and the source), which the roadmap lists as optional, and EPUB export.

## Run it for development

You need Python 3.12 or newer, [pandoc](https://pandoc.org/installing.html) 3.9 and [Typst](https://github.com/typst/typst/releases) 0.14 on your `PATH`. LaTeX builds also need TeX Live with `latexmk` and LuaLaTeX; without them, everything else works. The layouts' fonts go to `/usr/local/share/docwizard/fonts` with `sudo python3 -I deploy/fetch_tools.py --fonts-only` (or anywhere with `--fonts DIR` and `DOCWIZARD_FONTS_DIR=DIR`); without them, Typst falls back to its own fonts.

```sh
python -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install -e '.[dev]'

python -m docwizard create-admin you          # asks for a password
DOCWIZARD_RENDER_SPAWN_WORKER=true python -m docwizard serve
```

Open <http://localhost:8080>. The app serves previews from `http://127.0.0.1:8080/_preview`, a different origin, so keep using `localhost` for the app. Data goes to `./data/`.

`DOCWIZARD_RENDER_SPAWN_WORKER=true` starts the render worker as a child process. It runs jobs as your own user then; per-job users need the worker to start as root, as it does in the container setup.

Every setting can come from `docwizard.toml` (see [deploy/docwizard.example.toml](deploy/docwizard.example.toml)) or from an environment variable named `DOCWIZARD_<SECTION>_<KEY>`. `python -m docwizard check` reports missing tools and directories.

Some features need configuration:

- **Mail** for password resets and invitations: an `[smtp]` section. Without it, admins copy the links from the admin area and pass them on.
- **Login providers**: one `[[oidc]]` block per provider. Register `<public_url>/login/oidc/<id>/callback` as the redirect URI.
- **The secret key** that encrypts stored credentials is created at `<data_dir>/secret.key` on first start, or read from `security.secret_key_file`. Back it up separately from the data: without it, stored passwords and keys can't be read.
- **Sync servers in your own network** (such as `nextcloud.lan`) need to be listed in `security.allow_private_outbound` or in the admin settings.

## Deploy

[deploy/compose.yaml](deploy/compose.yaml) runs three containers: the app, the render worker (no network, read-only root, only the capabilities it needs to switch users) and Caddy, which serves static assets and published sites and forwards app requests to the app's Unix socket.

Prebuilt multi-arch images (`linux/amd64`, `linux/arm64`) are published to GitHub Container Registry (`ghcr.io/loredcast/docwizard-*`):
- `main` commits publish rolling tags (`:main` and `:sha-<commit>`).
- `nightly` commits publish nightly channel tags (`:nightly`, `:nightly-YYYYMMDD`, and `:sha-<commit>`).
- `v*` git tags publish release tags (`:<version>`, `:latest`).

By default, Compose pulls `:main` and listens on two local HTTP ports (`127.0.0.1:8080` for the app and `127.0.0.1:8081` for the publish/preview origin) so an external reverse proxy on the same host or another host can handle TLS and public domains without needing a repo clone:

```sh
curl -fsSLO https://raw.githubusercontent.com/LoredCast/DocWizard/main/deploy/compose.yaml
curl -fsSL https://raw.githubusercontent.com/LoredCast/DocWizard/main/deploy/docwizard.example.toml -o docwizard.toml
# Edit docwizard.toml with your https:// URLs, then point your external TLS proxy
# (forwarding Host, X-Forwarded-Proto and WebSockets):
#   write.example.org -> <host>:8080
#   pub.example.org   -> <host>:8081
# Set DOCWIZARD_TAG=nightly (or latest / 0.1.0) to switch channels instead of :main.
docker compose up -d
docker compose exec app python -m docwizard create-admin you
```

From a local checkout, run `docker compose -f deploy/compose.yaml up -d` (add `-f deploy/compose.nightly.yaml` or `DOCWIZARD_TAG=nightly` for the nightly channel, `--build` to build locally, and override `DOCWIZARD_APP_LISTEN` / `DOCWIZARD_PUBLISH_LISTEN` / `DOCWIZARD_TRUSTED_PROXIES` if needed).

To let the bundled Caddy terminate TLS directly on ports `80` and `443`, opt in with [deploy/compose.ssl.yaml](deploy/compose.ssl.yaml), [deploy/Caddyfile.ssl](deploy/Caddyfile.ssl), and bare domain names:

```sh
DOCWIZARD_APP_HOST=write.example.org DOCWIZARD_PUBLISH_HOST=pub.example.org \
  docker compose -f deploy/compose.yaml -f deploy/compose.ssl.yaml up -d
```

The worker image includes TeX Live for LaTeX builds. Build it with `--build-arg WITH_LATEX=0` for a much smaller image without them.

Published sites and previews are served from the publish host. In the Compose setup Caddy serves them from the publish directory; with `serve_static = true` the app serves them itself, which is fine for development.

For a machine without containers, [deploy/systemd/](deploy/systemd) has units for the app and the worker; put any reverse proxy in front that forwards both hosts to the app's socket.

Back up with `python -m docwizard backup <dir>` and restore with `python -m docwizard restore <dir>`.

## Tests

```sh
pytest
```

The suite has unit tests, property tests for the changeset and merge code, a cross-check of the Python changeset port against CodeMirror's own implementation (needs Node), adversarial protocol tests, and integration tests for collaboration, crash recovery, backup, sharing, offline merges, sync (against a WebDAV server in the test), Zotero, OIDC, publishing, two-step login, the admin area, search, the German interface and rendering. Rendering tests run when pandoc is installed, LaTeX tests when latexmk is; point `DOCWIZARD_TEST_TOOLS_PATH` at a directory with specific pandoc and Typst binaries to test those versions.

## Repository

| Path | What |
|---|---|
| `server/docwizard/` | The app: HTTP, auth, projects, collaboration, rendering, citations, sync, publishing, admin |
| `server/docwizard/locale/` | Interface translations, keyed by the English text |
| `worker/` | The render worker (standard library only) |
| `web/` | Stylesheet, ES modules and the vendored CodeMirror and Temml bundles |
| `layouts/` | Built-in layouts |
| `csl/` | Bundled citation styles (CC BY-SA 3.0, from the CSL project) |
| `filters/` | Built-in Lua filters (cross-references, page breaks, word count) |
| `deploy/` | Dockerfiles, Compose, Caddy and systemd |
| `scripts/vendor.sh` | Rebuilds the vendored frontend bundles |

## License

See [LICENSE](LICENSE).
