"""The writing helper (§8.10): an opt-in language model that can only propose suggestions.

The server's admin names an OpenAI-compatible endpoint (``[assist]``: Ollama, vLLM, llama.cpp's
server or a hosted API); each person turns the helper on in their settings. The editor sends a
passage with what to do with it; the app asks the model and files its answer as an ordinary
suggestion thread by that person, marked as the helper's, on exactly that passage. The text itself
never changes: an editor accepts or rejects the suggestion as any other.
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit

from .. import notifications
from ..auth.throttle import TokenBucket
from ..collab.authority import PushError
from ..http.request import HTTPError, Request, bad_request
from ..http.response import Response, json_response
from ..netpolicy import FetchError, Policy, PolicyError
from .comments import MAX_SUGGESTION, _announce, _author, _int
from .projects import document_access

TASKS = {
    "proofread": "Correct spelling, grammar and punctuation. Change nothing else.",
    "clearer": "Make the passage clearer and easier to read, keeping its meaning, tone and length.",
    "shorter": "Make the passage shorter without losing anything important.",
    "formal": "Make the passage more formal.",
    "simpler": "Rewrite the passage in simpler language.",
}
SYSTEM = (
    "You are an editor helping an author revise a passage of a Pandoc Markdown document. "
    "Answer with the revised passage only: no introduction, no explanation, no quotes or code fences around it. "
    "Keep the passage's language, its Markdown markup, citations like [@key], footnotes, math and links exactly as "
    "they are unless the task is about them. If nothing needs to change, answer with the passage unchanged."
)
MAX_INSTRUCTION = 500


def enabled(app) -> bool:
    cfg = app.cfg.assist
    return bool(cfg.url and cfg.model)


def opted_in(user) -> bool:
    return bool(user and not getattr(user, "guest", False) and user.preferences.get("assist") is True)


def _clean(answer: str, passage: str) -> str:
    """The model's answer as replacement text: fences and quotes some models add anyway removed, and
    the passage's own leading and trailing whitespace kept."""
    text = answer.strip()
    m = re.fullmatch(r"```[\w-]*\n(.*?)\n?```", text, re.S)
    if m:
        text = m.group(1).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'" and passage.strip()[:1] != text[0]:
        text = text[1:-1]
    lead = passage[:len(passage) - len(passage.lstrip())]
    trail = passage[len(passage.rstrip()):]
    return lead + text + trail


def register(app) -> None:
    r = app.router
    bucket = TokenBucket(max(1, app.cfg.assist.per_user_rpm), max(1, app.cfg.assist.per_user_rpm) / 60)

    def ask(passage: str, task: str, instruction: str, lang: str | None) -> str:
        cfg = app.cfg.assist
        url = cfg.url.rstrip("/") + "/chat/completions"
        what = TASKS.get(task) or instruction
        if task in TASKS and instruction:
            what += f" Also: {instruction}"
        user = f"Task: {what}\n" + (f"Language: {lang}\n" if lang else "") + f"\nPassage:\n{passage}"
        payload = {"model": cfg.model, "temperature": 0.2, "stream": False,
                   "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        # The admin chose this endpoint, so it may be on the internal network.
        policy = Policy([*app.policy("allow_private_outbound"), urlsplit(url).hostname or ""], resolver=app.net_resolver)
        try:
            resp = policy.fetch("POST", url, body=json.dumps(payload).encode(), headers=headers,
                                timeout=cfg.timeout_seconds, max_bytes=2_000_000)
        except (FetchError, PolicyError) as exc:
            raise HTTPError(502, "assist_unreachable", f"The writing helper can't be reached: {exc}") from None
        if resp.status != 200:
            raise HTTPError(502, "assist_error", f"The writing helper answered {resp.status}.")
        try:
            content = json.loads(resp.body)["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise HTTPError(502, "assist_error", "The writing helper sent something unexpected.") from None
        if not isinstance(content, str) or not content.strip():
            raise HTTPError(502, "assist_error", "The writing helper had no answer.")
        return content

    async def suggest(req: Request) -> Response:
        if not enabled(app):
            raise HTTPError(404, "not_configured", "This server has no writing helper.")
        if not opted_in(req.user):
            raise HTTPError(403, "assist_off", "Turn the writing helper on in your settings first.")
        doc, project, _ = await document_access(app, req, req.param("id"), "comment")
        body = await req.json_object(64 * 1024)
        passage, task = body.get("text"), str(body.get("task") or "")
        instruction = str(body.get("instruction") or "").strip()[:MAX_INSTRUCTION]
        if not isinstance(passage, str) or not passage.strip():
            raise bad_request("Select the passage to work on.")
        if len(passage) > app.cfg.assist.max_chars:
            raise bad_request(f"Select at most {app.cfg.assist.max_chars:,} characters at a time.")
        if task not in TASKS and not (task == "custom" and instruction):
            raise bad_request("Choose what the helper should do.")
        version, frm, to = _int(body.get("version")), _int(body.get("from")), _int(body.get("to"))
        if not bucket.allow(req.actor_key):
            raise HTTPError(429, "rate_limited", "Asking the helper too often. Wait a moment.")
        answer = await asyncio.to_thread(ask, passage, task, instruction, project["lang"] or None)
        suggestion = _clean(answer, passage)[:MAX_SUGGESTION]
        if suggestion == passage:
            return json_response({"unchanged": True})
        uid, gid, name = _author(req)
        what = TASKS.get(task, instruction) if task in TASKS else instruction
        note = f"Suggested by the writing helper ({app.cfg.assist.model}): {what}"
        notified: list[int] = []

        def insert(c, a, b, v, quote):
            if quote != passage:
                raise PushError("changed", "The passage changed while the helper worked. Try again.")
            cid = c.execute(
                "INSERT INTO comments(doc_id, author_user_id, author_guest_id, author_name, body, quote, anchor_from,"
                " anchor_to, anchor_version, suggestion) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (doc["id"], uid, gid, name, note, quote, a, b, v, suggestion)).lastrowid
            c.execute("UPDATE comments SET thread_id = id WHERE id = ?", (cid,))
            notified.extend(notifications.record(c, project_id=project["id"], doc_id=doc["id"], comment_id=cid,
                                                 thread_id=cid, actor_user_id=uid, actor=name, body=note))
            return cid

        room = await app.rooms.get(doc["id"])
        try:
            result = await room.call(lambda: room.add_thread(version, frm, to, insert))
        except PushError as exc:
            raise HTTPError(409, exc.code, exc.message) from None
        _announce(room, doc["id"], notified)
        return json_response({**result, "suggestion": suggestion}, 201)

    r.add("POST", "/api/documents/{id}/assist", suggest)
