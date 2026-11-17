"""Fountain screenplays: import on upload, export from the script layouts (§9.3)."""

from docwizard.projects import fountain
from tests.integration.test_collab import new_project, open_editor
from tests.integration.test_languages import needs_pandoc, pandoc
from tests.integration.test_render import wait_job

SCRIPT = """Title:
    _**BRICK & STEEL**_
    _**FULL RETIRED**_
Credit: Written by
Author: Stu Maschwitz
Source: Story by KTM
Draft date: 1/20/2012
Contact:
    Next Level Productions
    1588 Mission Dr.
    Solvang, CA 93463

EXT. BRICK'S PATIO - DAY #1#

A gorgeous day.  The sun is shining.  But BRICK BRADDOCK, retired police detective, is sitting quietly, contemplating -- something.

The SCREEN DOOR slides open and DICK STEEL, his former partner and fellow retiree, emerges with two cold beers.

STEEL
Beer's ready!

BRICK
Are they cold?

STEEL ^
Does a bear crap in the woods?

[[A note for the director.]]

/* An old idea,
cut from the draft. */

## Middle

= Steel finds the guns.

.FLASHBACK

@McCLANE
(beat)
Yippee ki-yay! Costs $5.

- not a list
1. not a number either
    indented action is not code
!INT. A forced action line

~Willy the whale

_Underlined_ and *italic*.

===

> BURN TO WHITE.

> THE END <
"""


def test_fountain_to_markdown():
    md = fountain.to_markdown(SCRIPT)
    head, body = md.split("\n---\n", 1)
    assert 'title: |\n  BRICK & STEEL\n  FULL RETIRED' in head
    assert 'author: "Stu Maschwitz"' in head and 'draft: "1/20/2012"' in head
    assert "contact: |\n  Next Level Productions\n  1588 Mission Dr." in head
    assert "Credit" not in head and "Source" not in head
    assert "EXT. BRICK'S PATIO - DAY\n" in body and "#1#" not in body
    assert "STEEL ^\nDoes a bear" in body
    assert "<!-- A note for the director. -->" in body and "<!-- An old idea,\ncut from the draft. -->" in body
    assert "<!-- ## Middle -->" in body and "<!-- Steel finds the guns. -->" in body
    assert ".FLASHBACK" in body and "@McCLANE\n(beat)\nYippee ki-yay! Costs \\$5." in body
    assert "\\- not a list" in body and "1\\. not a number" in body and "\nindented action is not code" in body
    assert "INT. A forced action line" in body and "*Willy the whale*" in body
    assert "[Underlined]{.underline} and *italic*." in body
    assert "\\newpage" in body and "> BURN TO WHITE." in body and "> THE END <" in body


@needs_pandoc
def test_imported_fountain_reads_as_a_script():
    md = fountain.to_markdown(SCRIPT)
    out = pandoc(md, "screenplay", to="html")
    for cls in ("scene-heading", "action", "character", "parenthetical", "dialogue", "dual-dialogue", "centered"):
        assert f'class="{cls}"' in out, cls
    assert "McCLANE" in out and "$5" in out and "<li>" not in out and "<pre" not in out


def test_markdown_to_fountain_and_back():
    md = ('---\ntitle: "Harbour"\nauthor:\n  - name: Ana Ruiz\n  - Ben Okafor\ndraft: Second draft\n'
          'contact: |\n  Ana Ruiz\n  ana@example.org\n---\n\n# ACT ONE\n\n## Verhörzimmer – innen / Tag\n\n'
          'EXT. HARBOUR - DAWN\n\nGulls _wheel_ and [scream]{.underline}.\n\n<!-- fix this -->\n\n'
          'MARA\n(quietly)\nYou came back.\n\n\\newpage\n\n> THE END <\n')
    out = fountain.from_markdown(md)
    assert out.startswith("Title: Harbour\nCredit: Written by\nAuthors:\n    Ana Ruiz\n    Ben Okafor\n"
                          "Draft date: Second draft\nContact:\n    Ana Ruiz\n    ana@example.org\n\n")
    assert "# ACT ONE\n\n.Verhörzimmer – innen / Tag\n\nEXT. HARBOUR - DAWN" in out
    assert "Gulls *wheel* and _scream_." in out and "[[fix this]]" in out and "\n===\n" in out
    assert "MARA\n(quietly)\nYou came back." in out
    back = fountain.to_markdown(out)
    assert "title: \"Harbour\"" in back and "MARA\n(quietly)\nYou came back." in back
    assert "# ACT ONE" in back and ".Verhörzimmer – innen / Tag" in back


async def test_upload_and_export_fountain(client, app):
    pid = await new_project(client)
    r = await client.request("PUT", f"/api/projects/{pid}/assets/brick.fountain", body=SCRIPT.encode())
    assert r.status == 201 and r.json()["type"] == "document", r.text
    snap = await open_editor(client, pid, "brick.md")
    assert snap["text"].startswith("---\ntitle: |") and "STEEL ^" in snap["text"]
    # Fountain export is offered for script layouts and works without the render worker.
    assert (await client.request("PATCH", f"/api/projects/{pid}", json={"layout": "screenplay"})).status == 200
    page = await client.get(f"/p/{pid}/brick.md")
    assert 'value="fountain"' in page.text
    r = await client.post(f"/api/projects/{pid}/export", json={"doc": snap["doc"]["id"], "target": "fountain"})
    data = await wait_job(client, r.json()["job"])
    assert data["status"] == "ok", data
    out = (await client.get(data["url"])).text
    assert out.startswith("Title:\n    BRICK & STEEL\n    FULL RETIRED\nCredit: Written by\nAuthor: Stu Maschwitz")
    assert "STEEL ^\nDoes a bear crap in the woods?" in out and "[[A note for the director.]]" in out
    assert "Costs $5." in out
