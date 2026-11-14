"""Citation library from project files (§10.2).

CSL-JSON files are used directly; BibTeX/BibLaTeX and RIS files are
converted to CSL-JSON once per content hash with a sandboxed ``bib2csl``
job. The editor gets a compact list for autocomplete and citation chips.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
from pathlib import Path

from ..logs import fields
from ..render import jobs
from ..render.worker_client import WorkerUnavailable

log = logging.getLogger("docwizard.citations")

CONVERT = {".bib": "biblatex", ".ris": "ris"}
MAX_ITEMS = 20_000


def summarize(item: dict) -> dict | None:
    key = item.get("id")
    if not isinstance(key, str) or not key:
        return None
    names = item.get("author") or item.get("editor") or []
    authors = []
    for n in names[:3] if isinstance(names, list) else []:
        if isinstance(n, dict):
            authors.append(n.get("family") or n.get("literal") or n.get("given") or "")
    if isinstance(names, list) and len(names) > 3:
        authors.append("et al.")
    year = ""
    issued = item.get("issued")
    if isinstance(issued, dict):
        parts = issued.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            year = str(parts[0][0])
        elif isinstance(issued.get("literal"), str):
            year = issued["literal"]
    title = item.get("title") if isinstance(item.get("title"), str) else ""
    container = item.get("container-title") if isinstance(item.get("container-title"), str) else ""
    return {"id": key, "authors": [a for a in authors if a], "year": year, "title": title[:300],
            "container": container[:200], "type": item.get("type", "")}


def label(summary: dict) -> str:
    authors = summary["authors"]
    if not authors:
        name = summary["title"][:30] or summary["id"]
    elif len(authors) == 1:
        name = authors[0]
    elif len(authors) == 2 and authors[1] != "et al.":
        name = f"{authors[0]} and {authors[1]}"
    else:
        name = f"{authors[0]} et al."
    return f"{name} {summary['year']}".strip()


class CitationLibrary:
    def __init__(self, app):
        self.app = app
        self.cache_dir = app.cfg.data_dir / "citations"

    async def _convert(self, path: str, text: str, fmt: str) -> list:
        digest = hashlib.sha256(fmt.encode() + b"\0" + text.encode()).hexdigest()
        cached = self.cache_dir / f"{digest}.json"
        try:
            return json.loads(cached.read_text())
        except (OSError, ValueError):
            pass
        renderer = self.app.renderer
        job_id = jobs.new_job_id()
        job_dir = self.app.cfg.jobs_dir / "run" / job_id
        name = "input" + Path(path).suffix

        def prepare():
            (job_dir / "src").mkdir(parents=True)
            (job_dir / "out").mkdir()
            (job_dir / "src" / name).write_text(text)
            (job_dir / "job.json").write_text(json.dumps({
                "id": job_id, "kind": "bib2csl", "limits": {"cpu_seconds": 30, "memory_mb": 1024, "wall_seconds": 30},
                "steps": [{"tool": "pandoc", "args": ["--sandbox", "--from", fmt, "--to", "csljson", name,
                                                      "-o", "../out/refs.json"]}]}))

        try:
            await asyncio.to_thread(prepare)
            reply = await renderer.worker.run(job_id, 30)
            if reply.get("status") != "ok":
                log.warning("bibliography conversion failed", extra=fields(path=path, diagnostics=reply.get("diagnostics")))
                return []
            data = (job_dir / "out" / "refs.json").read_text()
            items = json.loads(data)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cached.write_text(data)
            return items if isinstance(items, list) else []
        except (WorkerUnavailable, OSError, ValueError):
            return []
        finally:
            await asyncio.to_thread(shutil.rmtree, job_dir, True)

    async def items(self, project_id: str) -> tuple[list[dict], str]:
        snap, _ = await self.app.renderer.snapshot(project_id)
        out: dict[str, dict] = {}
        for path in sorted(snap.texts):
            text = snap.texts[path]
            suffix = Path(path).suffix.lower()
            raw: list = []
            if suffix == ".json":
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                if isinstance(data, dict) and isinstance(data.get("items"), list):
                    data = data["items"]
                if not (isinstance(data, list) and data and all(isinstance(i, dict) and "id" in i for i in data[:5])):
                    continue
                raw = data
            elif suffix in CONVERT:
                raw = await self._convert(path, text, CONVERT[suffix])
            for item in raw[:MAX_ITEMS]:
                s = summarize(item) if isinstance(item, dict) else None
                if s is not None and s["id"] not in out:
                    s["label"] = label(s)
                    s["source"] = path
                    out[s["id"]] = s
        items = list(out.values())
        etag = '"' + hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()[:32] + '"'
        return items, etag
