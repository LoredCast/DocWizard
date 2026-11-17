"""Word files in and out: comments and tracked changes become threads and suggestions, and
open threads go back out as Word comments (§8.11)."""

import io
import shutil
import subprocess
import zipfile

import pytest

from docwizard.projects import docx_import
from tests.integration.test_collab import new_project
from tests.integration.test_render import _pandoc_ok, rapp, rclient, wait_job  # noqa: F401

SOURCE = """---
title: Field Notes
---

# Intro

The [quick]{.deletion author="Ben" date="2024-01-02T10:00:00Z"}[slow]{.insertion author="Ben"} brown fox \
[Is this right?]{.comment-start id="0" author="Cora" date="2024-01-03T09:00:00Z"}jumps over[]{.comment-end id="0"} \
the dog.[ Again.]{.insertion author="Ben"} It was [very]{.deletion author="Ben"} late.

[Agreed.]{.comment-start id="1" author="Ana"}[Me too.]{.comment-start id="2" author="Ben"}Second[]{.comment-end id="1"}[]{.comment-end id="2"} paragraph, café ☕ 𝄞.

![A map](map.png)
"""

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d4944415478da6364f8ffbf1e000502027fbd2b3a3b0000000049454e44ae426082")


def make_docx(tmp_path) -> bytes:
    (tmp_path / "map.png").write_bytes(PNG)
    (tmp_path / "in.md").write_text(SOURCE)
    subprocess.run(["pandoc", "in.md", "-o", "in.docx"], cwd=tmp_path, check=True)
    return (tmp_path / "in.docx").read_bytes()


def test_parse_markers():
    O, C = docx_import.O, docx_import.C
    md = (f"A {O}D1{C}red{O}/D1{C}{O}I2{C}blue{O}/I2{C} car {O}C7{C}drove{O}/C7{C} off{O}I3{C} fast{O}/I3{C}.\n\n"
          '<!-- dw-import {"comments":[{"id":"7","author":"Cy","date":"","text":"Really?"}],'
          '"changes":[{"kind":"D","author":"Bo"},{"kind":"I","author":"Bo"},{"kind":"I","author":"Al"}]} -->\n')
    text, threads = docx_import.parse(md)
    assert text == "A red car drove off.\n"
    got = [(text[t.start:t.end], t.suggestion, t.author, t.body) for t in threads]
    assert got == [("red", "blue", "Bo", ""), ("drove", None, "Cy", "Really?"), ("off", "off fast", "Al", "")]


def test_comment_markers_avoid_block_syntax():
    text = "---\ntitle: X\n---\n\n# Heading here\n\n- item one\n\n```\ncode\n```\n"
    h = text.index("# Heading")
    code = text.index("code")
    items = [{"start": h, "end": h + 9, "comments": [{"author": "A", "date": "", "body": "Fix [this] *now*"}]},
             {"start": code, "end": code + 4, "comments": [{"author": "B", "date": "", "body": "skip"}]},
             {"start": 0, "end": 3, "comments": [{"author": "C", "date": "", "body": "front matter"}]}]
    out = docx_import.comment_markers(text, items)
    assert '# [Fix \\[this\\] \\*now\\*]{.comment-start id="0" author="A" date=""}Heading[]{.comment-end id="0"} here' \
        in out
    assert "skip" not in out and "front matter" not in out
    assert docx_import.from_utf16("a𝄞b", [0, 1, 3, 4]) == [0, 1, 2, 3]


@pytest.mark.skipif(not _pandoc_ok(), reason="needs pandoc")
async def test_docx_round_trip(rapp, rclient, tmp_path):
    pid = await new_project(rclient)
    data = make_docx(tmp_path)
    r = await rclient.request("PUT", f"/api/projects/{pid}/assets/notes.docx", body=data)
    assert r.status == 201, r.text
    got = r.json()
    assert got["path"] == "notes.md" and got["comments"] == 2 and got["suggestions"] == 3, got
    doc = got["id"]
    room = await rapp.rooms.get(doc)
    text = room.text()
    # Word's original wording, with the image next to it.
    assert "The quick brown fox jumps over the dog. It was very late." in text, text
    assert "notes-media/" in text and "title: Field Notes" in text
    tree = (await rclient.get(f"/api/projects/{pid}/tree")).json()["tree"]
    assert "notes-media" in str(tree)

    threads = (await rclient.get(f"/api/documents/{doc}/comments")).json()["threads"]
    by_quote = {t["quote"]: t for t in threads}
    assert by_quote["quick"]["suggestion"] == "slow" and by_quote["quick"]["comments"][0]["author"] == "Ben (Word)"
    assert by_quote["very"]["suggestion"] == ""
    # (pandoc's own DOCX writer drops the space at the start of the insertion; Word keeps it)
    assert by_quote["dog."]["suggestion"] in ("dog. Again.", "dog.Again.")
    assert by_quote["jumps over"]["comments"][0]["body"] == "Is this right?"
    assert [c["body"] for c in by_quote["Second"]["comments"]] == ["Agreed.", "Me too."]

    # Accepting a suggestion edits the text, as accepting the change in Word would.
    r = await rclient.post(f"/api/documents/{doc}/comments/{by_quote['quick']['id']}/suggestion",
                           json={"decision": "accept"})
    assert r.status == 200, r.text
    assert "The slow brown fox" in room.text()

    # Export: open threads become Word comments again; a second upload gets its own name.
    r = await rclient.post(f"/api/projects/{pid}/export", json={"doc": doc, "target": "docx"})
    job = await wait_job(rclient, r.json()["job"])
    assert job["status"] == "ok", job
    out = (await rclient.get(job["url"])).body
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        comments = z.read("word/comments.xml").decode()
    assert "Is this right?" in comments and "Cora (Word)" in comments and "Me too." in comments
    assert "Suggested: “dog." in comments and "Suggested: “slow”" not in comments
    again = await rclient.request("PUT", f"/api/projects/{pid}/assets/notes.docx", body=out)
    assert again.status == 201 and again.json()["path"] == "notes-2.md"
    assert again.json()["comments"] >= 4


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="needs pandoc")
async def test_bad_docx_is_refused(rapp, rclient):
    pid = await new_project(rclient)
    r = await rclient.request("PUT", f"/api/projects/{pid}/assets/broken.docx", body=b"not a zip")
    assert r.status == 400 and r.json()["error"]["code"] == "bad_docx"
