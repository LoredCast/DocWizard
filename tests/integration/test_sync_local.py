"""Sync with a folder on the server (§12.6) and with a git repository (§12.7)."""

import os
import shutil

import pytest

from tests.fakes.githttp import GitServer
from tests.integration.test_collab import new_project
from tests.integration.test_sync import doc_of, edit, run, text_of

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# -- folder -----------------------------------------------------------------------

@pytest.fixture
def shared(app, tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    app.cfg.sync.folder_roots = [str(root)]
    return root


async def test_folder_sync_both_ways(client, app, shared):
    pid = await new_project(client)
    status = (await client.get(f"/api/projects/{pid}/sync")).json()
    assert status["options"]["folder"] and status["options"]["folderRoots"] == [str(shared)]
    r = await client.request("PUT", f"/api/projects/{pid}/sync", json={"kind": "folder", "folder": "Thesis"})
    assert r.status == 201, r.text
    assert r.json()["target"]["kind"] == "folder" and r.json()["target"]["remotePath"] == "Thesis"
    report = await run(client, pid)
    assert sorted(report["uploaded"]) == ["paper.md", "references.json"]
    folder = shared / "Thesis"
    assert (folder / "paper.md").read_text() == await text_of(app, pid, "paper.md")
    assert not any((await run(client, pid)).values())

    # Edited in another program, then here.
    (folder / "paper.md").write_text((folder / "paper.md").read_text().replace("Write here.", "Written in vim."))
    (folder / "notes").mkdir()
    (folder / "notes" / "ideas.md").write_text("# Ideas\n")
    report = await run(client, pid)
    assert sorted(report["downloaded"]) == ["notes/ideas.md", "paper.md"]
    assert "Written in vim." in await text_of(app, pid, "paper.md")
    await edit(app, pid, "paper.md", lambda t: t + "\nAdded in DocWizard.\n")
    assert (await run(client, pid))["uploaded"] == ["paper.md"]
    assert (folder / "paper.md").read_text().endswith("Added in DocWizard.\n")

    # Overlapping edits become a conflict, with the other text kept next to the file.
    (folder / "paper.md").write_text((folder / "paper.md").read_text().replace("Written in vim.", "Folder words."))
    await edit(app, pid, "paper.md", lambda t: t.replace("Written in vim.", "Local words."))
    report = await run(client, pid)
    assert report["conflicts"] == ["paper.md"]
    await run(client, pid)
    copies = [p.name for p in folder.iterdir() if "(conflict" in p.name]
    assert len(copies) == 1 and "Folder words." in (folder / copies[0]).read_text()

    # Deleted in the folder: moved to the trash here; deleted here: removed there, with its empty folder.
    os.remove(folder / copies[0])
    assert (await run(client, pid))["deleted_local"] == [copies[0]]
    doc = await doc_of(app, pid, "notes/ideas.md")
    assert (await client.request("DELETE", f"/api/documents/{doc['id']}")).status == 200
    assert (await run(client, pid))["deleted_remote"] == ["notes/ideas.md"]
    assert not (folder / "notes").exists()


async def test_folder_must_stay_inside_the_shared_folders(client, app, shared, tmp_path):
    pid = await new_project(client)
    for folder, message in [(str(tmp_path / "elsewhere"), "has to be inside"), ("../escape", "has to be inside"),
                            ("", "not the shared folder itself"), (".hidden", "hidden")]:
        r = await client.request("PUT", f"/api/projects/{pid}/sync", json={"kind": "folder", "folder": folder})
        assert r.status == 400 and message in r.text, (folder, r.text)
    # An absolute path inside the root is fine; a second project can't take the same folder.
    r = await client.request("PUT", f"/api/projects/{pid}/sync", json={"kind": "folder", "folder": str(shared / "A")})
    assert r.status == 201, r.text
    other = await new_project(client, "Other")
    r = await client.request("PUT", f"/api/projects/{other}/sync", json={"kind": "folder", "folder": "A/sub"})
    assert r.status == 400 and "Another project" in r.text
    # A symlink inside the folder is never followed.
    (shared / "A").mkdir(exist_ok=True)
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "passwd.md").write_text("secret")
    os.symlink(outside, shared / "A" / "link")
    os.symlink(outside / "passwd.md", shared / "A" / "file.md")
    report = await run(client, pid)
    assert "link/passwd.md" not in report["downloaded"] and "file.md" not in report["downloaded"]
    # Without shared folders, folder sync is off.
    app.cfg.sync.folder_roots = []
    assert not (await client.get(f"/api/projects/{pid}/sync")).json()["options"]["folder"]
    r = await client.post(f"/api/projects/{pid}/sync/run")
    assert "isn't enabled" in r.json()["report"]["error"]


# -- git --------------------------------------------------------------------------------

@pytest.fixture
async def repo(app, tmp_path, monkeypatch):
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    server = GitServer(tmp_path / "git").start()
    app.net_resolver = lambda host, port: ["127.0.0.1"]
    await app.set_setting("allow_private_outbound", ["git.test"])
    yield server
    server.stop()


async def connect_git(client, pid, repo, **extra):
    body = {"kind": "git", "url": repo.url, "branch": "main", "username": repo.user, "password": repo.token, **extra}
    return await client.request("PUT", f"/api/projects/{pid}/sync", json=body)


@needs_git
async def test_git_sync_commits_pulls_and_merges(client, app, repo):
    pid = await new_project(client)
    r = await connect_git(client, pid, repo, password=repo.token[::-1])
    assert r.status == 400 and "didn't accept" in r.text
    assert repo.token not in r.text and repo.token[::-1] not in r.text
    r = await connect_git(client, pid, repo)
    assert r.status == 201, r.text
    target = r.json()["target"]
    assert target["kind"] == "git" and target["branch"] == "main" and target["author"] == "Jane Doe"
    assert repo.token not in (await app.db.fetchone("SELECT secret_enc FROM sync_targets"))["secret_enc"]

    # The first run creates the branch in the empty repository.
    report = await run(client, pid)
    assert sorted(report["uploaded"]) == ["paper.md", "references.json"]
    assert repo.read("paper.md") == await text_of(app, pid, "paper.md")
    assert repo.log()[0][0] == "Jane Doe <docwizard@localhost>"
    assert repo.log()[0][1] == "Update paper.md and 1 other file"
    assert not any((await run(client, pid)).values())
    assert len(repo.log()) == 1  # nothing changed: no empty commit

    # Someone pushes from a laptop; DocWizard edits the same file elsewhere: both arrive.
    repo.commit({"paper.md": repo.read("paper.md").replace("Write here.", "Written on the train."),
                 "chapters/two.md": "# Two\n", ".github/ci.yml": "on: push\n"})
    await edit(app, pid, "paper.md", lambda t: t + "\nAdded in DocWizard.\n")
    report = await run(client, pid)
    assert "chapters/two.md" in report["downloaded"] and report["conflicts"] == []
    text = await text_of(app, pid, "paper.md")
    assert "Written on the train." in text and "Added in DocWizard." in text
    assert repo.read("paper.md") == text
    assert await doc_of(app, pid, ".github/ci.yml") is None  # hidden files stay in git only
    assert repo.read(".github/ci.yml") == "on: push\n"

    # Deleted on the laptop: moved to the trash here. Renamed here: a rename in git.
    repo.commit({"chapters/two.md": None})
    assert (await run(client, pid))["deleted_local"] == ["chapters/two.md"]
    paper = await doc_of(app, pid, "paper.md")
    r = await client.request("PATCH", f"/api/documents/{paper['id']}", json={"path": "thesis.md"})
    assert r.status == 200
    assert (await run(client, pid))["renamed"] == ["paper.md -> thesis.md"]
    assert repo.read("thesis.md") == text and repo.read("paper.md") is None
    assert all(authed for _, _, authed in repo.requests[-3:])


@needs_git
async def test_git_push_race_and_failures_lose_nothing(client, app, repo, monkeypatch):
    from docwizard.sync import git as gitsync

    pid = await new_project(client)
    assert (await connect_git(client, pid, repo)).status == 201
    await run(client, pid)

    # Someone pushes between DocWizard's fetch and its push: the commit is rebased onto theirs.
    original = gitsync.GitRemote.finish

    def finish_after_their_push(self, message):
        repo.commit({"other.md": "theirs\n"})
        monkeypatch.setattr(gitsync.GitRemote, "finish", original)
        return original(self, message)

    monkeypatch.setattr(gitsync.GitRemote, "finish", finish_after_their_push)
    await edit(app, pid, "paper.md", lambda t: t + "\nOurs.\n")
    await run(client, pid)
    assert repo.read("other.md") == "theirs\n" and repo.read("paper.md").endswith("Ours.\n")

    # A push that fails rolls the bases back: the next run pushes the same change.
    def broken(self, message):
        raise gitsync.GitError("git push failed: remote hung up")

    monkeypatch.setattr(gitsync.GitRemote, "finish", broken)
    await edit(app, pid, "paper.md", lambda t: t + "\nAfter the failure.\n")
    r = await client.post(f"/api/projects/{pid}/sync/run")
    assert "hung up" in r.json()["report"]["error"]
    monkeypatch.setattr(gitsync.GitRemote, "finish", original)
    await run(client, pid)
    assert repo.read("paper.md").endswith("After the failure.\n")

    # The clone is a cache: without it, the next run clones again and changes nothing.
    target = await app.db.fetchone("SELECT id FROM sync_targets")
    shutil.rmtree(app.cfg.data_dir / "sync-git" / str(target["id"]))
    before = len(repo.log())
    report = await run(client, pid)
    assert report["conflicts"] == [] and len(repo.log()) == before

    # Disconnecting removes the clone.
    assert (await client.request("DELETE", f"/api/projects/{pid}/sync")).status == 200
    assert not (app.cfg.data_dir / "sync-git" / str(target["id"])).exists()


@needs_git
async def test_git_addresses_are_checked(client, app, repo):
    pid = await new_project(client)
    for url, message in [("file:///etc", "https://"), ("ssh://git@github.com/a/b.git", "https://"),
                         ("https://jane:tok@github.com/a/b.git", "Leave the username"),
                         (repo.url + "?x=1", "? or #")]:
        r = await client.request("PUT", f"/api/projects/{pid}/sync", json={"kind": "git", "url": url})
        assert r.status == 400 and message in r.text, (url, r.text)
    r = await connect_git(client, pid, repo, branch="../main")
    assert r.status == 400 and "branch" in r.text
    r = await connect_git(client, pid, repo, authorName="Eve <evil>")
    assert r.status == 400
    # Private addresses need an admin's exception, as for WebDAV.
    await app.set_setting("allow_private_outbound", [])
    r = await connect_git(client, pid, repo)
    assert r.status == 400 and "private address" in r.text
