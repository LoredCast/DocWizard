"""The writing helper (§8.10): opt-in, and its answers only ever become suggestions."""

import json

import pytest

from docwizard import netpolicy
from docwizard.api import assist
from tests.integration.test_collab import new_project, open_editor


class FakeModel:
    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def __call__(self, allow=None, *, resolver=None):
        return self

    def fetch(self, method, url, *, headers=None, body=None, timeout=None, max_bytes=None):
        self.requests.append({"url": url, "headers": headers, "body": json.loads(body)})
        payload = {"choices": [{"message": {"role": "assistant", "content": self.answer}}]}
        return netpolicy.Response(200, {}, json.dumps(payload).encode(), url)


@pytest.fixture
def model(monkeypatch):
    fake = FakeModel("```\nThe keeper logged a quiet night.\n```")
    monkeypatch.setattr(assist, "Policy", fake)
    return fake


async def _settings(client, **form):
    r = await client.post("/settings/profile", form={"display_name": "Jane Doe", **form})
    assert r.status == 303, r.text


async def test_helper_files_a_suggestion_and_leaves_the_text(app, client, model):
    app.cfg.assist.url, app.cfg.assist.model, app.cfg.assist.api_key = "http://llm.internal/v1", "tiny", "sk-x"
    pid = await new_project(client)
    r = await client.post(f"/api/projects/{pid}/documents", json={"path": "n.md", "text": "The keper loged a quiet nite.\n"})
    assert r.status == 201
    snap = await open_editor(client, pid, "n.md")
    doc = snap["doc"]["id"]
    ask = {"text": "The keper loged a quiet nite.", "from": 0, "to": 29, "version": snap["version"], "task": "proofread"}

    # Off until the person turns it on.
    assert snap["assist"] is False
    r = await client.post(f"/api/documents/{doc}/assist", json=ask)
    assert r.status == 403 and r.json()["error"]["code"] == "assist_off"
    await _settings(client, assist_shown="1", assist="on")
    snap = await open_editor(client, pid, "n.md")
    assert snap["assist"] is True

    r = await client.post(f"/api/documents/{doc}/assist", json=ask)
    assert r.status == 201, r.text
    assert r.json()["suggestion"] == "The keeper logged a quiet night."
    sent = model.requests[0]
    assert sent["url"] == "http://llm.internal/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer sk-x" and sent["body"]["model"] == "tiny"
    assert "The keper loged a quiet nite." in sent["body"]["messages"][1]["content"]
    # A suggestion thread, marked as the helper's; the text is unchanged.
    threads = (await client.get(f"/api/documents/{doc}/comments")).json()["threads"]
    assert len(threads) == 1 and threads[0]["suggestion"] == "The keeper logged a quiet night."
    assert "writing helper (tiny)" in threads[0]["comments"][0]["body"]
    assert app.rooms.loaded(doc).text() == "The keper loged a quiet nite.\n"

    # A passage that isn't what the text says there is refused.
    r = await client.post(f"/api/documents/{doc}/assist", json={**ask, "text": "Something else entirely here."})
    assert r.status == 409 and r.json()["error"]["code"] == "changed"
    # An answer that changes nothing files nothing.
    model.answer = "The keper loged a quiet nite."
    r = await client.post(f"/api/documents/{doc}/assist", json=ask)
    assert r.json() == {"unchanged": True}
    # Turned off again.
    await _settings(client, assist_shown="1")
    assert (await client.post(f"/api/documents/{doc}/assist", json=ask)).status == 403


async def test_helper_needs_a_server_model(app, client, model):
    pid = await new_project(client)
    snap = await open_editor(client, pid)
    r = await client.post(f"/api/documents/{snap['doc']['id']}/assist", json={"text": "x", "task": "proofread"})
    assert r.status == 404


def test_clean_keeps_the_passages_spacing():
    assert assist._clean('"Hello."', "  Hallo.\n") == "  Hello.\n"
    assert assist._clean("```markdown\n*Hi*\n```", "*Hy*") == "*Hi*"
