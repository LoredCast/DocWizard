# DocWizard

A self-hostable editor for structured writing: term papers, theses, articles and reports. You write pandoc Markdown in the browser; **layouts** decide how the output looks. Exports go through pandoc, with Typst for PDF and citeproc for citations.

[ARCHITECTURE.md](ARCHITECTURE.md) is the design reference. This first version covers roadmap milestones M0 and M1 (reliable editing and submission-ready output), plus the parts of M2–M4 that the same code already provides.

## What works

- **Editing**: CodeMirror with pandoc Markdown (front matter, citations, math, footnotes, attributes, fenced divs), live preview of headings, emphasis, links, task lists, citation chips and MathML, citation autocomplete, heading folding, word count. A LaTeX mode for `.tex` files.
- **Saving that survives crashes**: every edit is committed to SQLite (`synchronous=FULL`) before it is acknowledged, then written to plain files in the project folder. On startup, the files are reconstructed from the database. Edits made to those files outside DocWizard are detected and reconciled, with real conflicts kept visible instead of blended.
- **Collaboration**: several tabs or people can edit the same document (OT with a central authority, compatible with `@codemirror/collab`). Unconfirmed edits are kept in IndexedDB, survive reloads and come back after offline periods as a merge with explicit conflicts.
- **Versions**: named versions, restore (the current text is kept as a version first).
- **Rendering**: a separate worker runs pandoc and Typst, each job as its own Unix user with resource limits. HTML preview on a separate preview origin through capability URLs; PDF preview; exports to PDF, DOCX, ODT, HTML, Markdown, Typst and LaTeX source, and a zip of the project.
- **Layouts**: `default` and `term-paper` (title page, numbered sections, 1.5 spacing), with options such as paper size, font size and spacing. Layouts can extend each other.
- **Citations**: from CSL-JSON, BibTeX or RIS files in the project, formatted Chicago author-date by default; cross-references to figures, tables and equations.
- **Accounts and projects**: local accounts, sessions, CSRF protection, project roles (owner, editor, commenter, viewer), uploads, backup and restore from the command line.

Not yet: WebDAV sync, Zotero, OIDC, sharing links and guests, comments, presence, publishing, the service worker for offline page loads, LaTeX builds and PDF.js. See the roadmap in ARCHITECTURE.md §21.

## Run it for development

You need Python 3.12 or newer, [pandoc](https://pandoc.org/installing.html) 3.9 and [Typst](https://github.com/typst/typst/releases) 0.14 on your `PATH`.

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

## Deploy

[deploy/compose.yaml](deploy/compose.yaml) runs three containers: the app, the render worker (no network, read-only root, only the capabilities it needs to switch users) and Caddy, which terminates TLS for two hosts, the app and the publish/preview host.

```sh
cp deploy/docwizard.example.toml deploy/docwizard.toml   # set your two URLs
# set DOCWIZARD_APP_HOST and DOCWIZARD_PUBLISH_HOST in deploy/compose.yaml
docker compose -f deploy/compose.yaml up -d --build
docker compose -f deploy/compose.yaml exec app python -m docwizard create-admin you
```

For a machine without containers, [deploy/systemd/](deploy/systemd) has units for the app and the worker; put any reverse proxy in front that forwards both hosts to the app's socket.

Back up with `python -m docwizard backup <dir>` and restore with `python -m docwizard restore <dir>`.

## Tests

```sh
pytest
```

The suite has unit tests, property tests for the changeset and merge code, a cross-check of the Python changeset port against CodeMirror's own implementation (needs Node), and integration tests for collaboration, crash recovery, backup and rendering. Rendering tests run when pandoc is installed; point `DOCWIZARD_TEST_TOOLS_PATH` at a directory with specific pandoc and Typst binaries to test those versions.

## Repository

| Path | What |
|---|---|
| `server/docwizard/` | The app: HTTP, auth, projects, collaboration, rendering, citations |
| `worker/` | The render worker (standard library only) |
| `web/` | Stylesheet, ES modules and the vendored CodeMirror and Temml bundles |
| `layouts/` | Built-in layouts |
| `filters/` | Built-in Lua filters (cross-references, page breaks, word count) |
| `deploy/` | Dockerfiles, Compose, Caddy and systemd |
| `scripts/vendor.sh` | Rebuilds the vendored frontend bundles |

## License

See [LICENSE](LICENSE).
