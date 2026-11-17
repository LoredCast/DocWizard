"""Add a reference by its DOI, ISBN or arXiv ID (§10.5).

DOIs (and arXiv IDs, through their DataCite DOIs 10.48550/arXiv.<id>) are resolved by content
negotiation at doi.org, which answers in CSL-JSON for Crossref and DataCite alike. ISBNs are
looked up in Open Library. Every request goes through the outbound request policy (§15).
The item gets a citation key like ``doe2020infrastructure`` and is appended to a CSL-JSON
bibliography file in the project.
"""

from __future__ import annotations

import json
import re
import unicodedata
from urllib.parse import quote

DOI = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/\S+)$", re.I)
ARXIV = re.compile(r"^(?:arxiv:\s*|https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/)?"
                   r"(\d{4}\.\d{4,5}|[a-z][a-z.\-]*/\d{7})(?:v\d+)?(?:\.pdf)?$", re.I)
STOP = {"a", "an", "the", "on", "of", "in", "and", "for", "to", "with", "der", "die", "das", "ein", "eine",
        "le", "la", "les", "un", "une", "el", "los"}
TIMEOUT = 15.0
MAX_BYTES = 2_000_000


class IdentifierError(ValueError):
    """The identifier isn't one we know, or nothing was found for it."""


def _isbn_ok(digits: str) -> bool:
    if len(digits) == 10:
        total = sum((10 - i) * (10 if c in "Xx" else int(c)) for i, c in enumerate(digits))
        return total % 11 == 0
    if len(digits) == 13 and digits.isdigit():
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(digits))
        return total % 10 == 0
    return False


def classify(raw: str) -> tuple[str, str]:
    """('doi' | 'isbn', normalized value) for what someone pasted."""
    s = raw.strip().strip("<>").rstrip(".,;")
    m = ARXIV.match(s)
    if m and not DOI.match(s):
        return "doi", f"10.48550/arXiv.{m.group(1)}"
    m = DOI.match(s)
    if m:
        return "doi", m.group(1)
    digits = re.sub(r"^(?:isbn(?:-1[03])?:?\s*)", "", s, flags=re.I)
    digits = re.sub(r"[\s\-]", "", digits)
    if re.fullmatch(r"\d{9}[\dXx]|\d{13}", digits) and _isbn_ok(digits):
        return "isbn", digits.upper()
    raise IdentifierError("That doesn't look like a DOI, ISBN or arXiv ID.")


def fetch_item(net, raw: str) -> dict:
    """Look the identifier up and return a CSL-JSON item (without an id). Blocking."""
    kind, value = classify(raw)
    if kind == "doi":
        resp = net.fetch("GET", "https://doi.org/" + quote(value, safe="/:;()._-"),
                         headers={"Accept": "application/vnd.citationstyles.csl+json"},
                         timeout=TIMEOUT, max_bytes=MAX_BYTES)
        if resp.status == 404:
            raise IdentifierError(f"No record for the DOI {value}.")
        if resp.status != 200:
            raise IdentifierError(f"doi.org answered {resp.status}. Try again later.")
        try:
            item = json.loads(resp.body)
        except ValueError:
            raise IdentifierError("doi.org sent something other than a reference.") from None
        if not isinstance(item, dict) or not item.get("title"):
            raise IdentifierError("doi.org sent something other than a reference.")
        return _clean(item)
    resp = net.fetch("GET", f"https://openlibrary.org/api/books?bibkeys=ISBN:{value}&format=json&jscmd=data",
                     headers={"Accept": "application/json"}, timeout=TIMEOUT, max_bytes=MAX_BYTES)
    if resp.status != 200:
        raise IdentifierError(f"Open Library answered {resp.status}. Try again later.")
    try:
        data = json.loads(resp.body).get(f"ISBN:{value}")
    except (ValueError, AttributeError):
        data = None
    if not data:
        raise IdentifierError(f"Open Library has no book with the ISBN {value}.")
    return _book(data, value)


def _book(d: dict, isbn: str) -> dict:
    item: dict = {"type": "book", "title": d.get("title", "")}
    if d.get("subtitle"):
        item["title"] += ": " + d["subtitle"]
    authors = [_name(a.get("name", "")) for a in d.get("authors", []) if a.get("name")]
    if authors:
        item["author"] = authors
    pubs = [p.get("name") for p in d.get("publishers", []) if p.get("name")]
    if pubs:
        item["publisher"] = pubs[0]
    places = [p.get("name") for p in d.get("publish_places", []) if p.get("name")]
    if places:
        item["publisher-place"] = places[0]
    year = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", d.get("publish_date", ""))
    if year:
        item["issued"] = {"date-parts": [[int(year.group(1))]]}
    if d.get("number_of_pages"):
        item["number-of-pages"] = str(d["number_of_pages"])
    item["ISBN"] = isbn
    return item


def _name(full: str) -> dict:
    full = " ".join(full.split())
    if "," in full:
        family, given = (p.strip() for p in full.split(",", 1))
    else:
        given, _, family = full.rpartition(" ")
    return {"family": family, "given": given} if given else {"literal": family}


KEEP = {"type", "title", "container-title", "collection-title", "author", "editor", "translator", "issued",
        "volume", "issue", "page", "publisher", "publisher-place", "DOI", "ISBN", "ISSN", "URL", "edition",
        "number", "genre", "event", "event-place", "abstract", "language", "number-of-pages", "archive",
        "container-title-short", "chapter-number"}


def _clean(item: dict) -> dict:
    """Keep what citation styles use; drop references lists, licences and the like."""
    out = {k: v for k, v in item.items() if k in KEEP}
    if isinstance(out.get("container-title"), list):
        out["container-title"] = out["container-title"][0] if out["container-title"] else ""
    if isinstance(out.get("title"), list):
        out["title"] = out["title"][0] if out["title"] else ""
    if isinstance(out.get("abstract"), str):
        out["abstract"] = re.sub(r"<[^>]+>", "", out["abstract"]).strip()[:4000]
    if out.get("type") == "article" and str(out.get("DOI", "")).lower().startswith("10.48550/arxiv."):
        out.setdefault("publisher", "arXiv")
        out["number"] = "arXiv:" + str(out["DOI"]).split(".", 2)[-1]
    if not out.get("type"):
        out["type"] = "article-journal"
    return out


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def make_key(item: dict, taken: set[str]) -> str:
    """doe2020infrastructure: the first author's family name, the year, the title's first word."""
    names = item.get("author") or item.get("editor") or []
    first = names[0] if names else {}
    family = re.sub(r"[^a-z]", "", _ascii(first.get("family") or first.get("literal") or "")) or "anon"
    parts = (item.get("issued") or {}).get("date-parts") or [[]]
    year = str(parts[0][0]) if parts and parts[0] else "nd"
    words = [re.sub(r"[^a-z0-9]", "", _ascii(w)) for w in str(item.get("title", "")).split()]
    word = next((w for w in words if w and w not in STOP), "")
    base = f"{family}{year}{word}"[:60]
    key, n = base, 0
    while key in taken:
        n += 1
        key = base + "abcdefghijklmnopqrstuvwxyz"[(n - 1) % 26] * (1 + (n - 1) // 26)
    return key


def add_to_file(text: str | None, item: dict) -> tuple[str, str, bool]:
    """Append *item* to a CSL-JSON file's text. Returns (new text, key, added). An item with the
    same DOI or ISBN that is already there is reused."""
    items = []
    if text and text.strip():
        try:
            items = json.loads(text)
        except ValueError:
            raise IdentifierError("The bibliography file isn't valid CSL-JSON, so nothing was added to it.") from None
        if not isinstance(items, list):
            raise IdentifierError("The bibliography file isn't a CSL-JSON list, so nothing was added to it.")
    for existing in items:
        if not isinstance(existing, dict):
            continue
        for field in ("DOI", "ISBN"):
            if item.get(field) and str(existing.get(field, "")).lower() == str(item[field]).lower():
                return text or "", str(existing.get("id")), False
    key = make_key(item, {str(i.get("id")) for i in items if isinstance(i, dict)})
    items.append({"id": key, **item})
    return json.dumps(items, ensure_ascii=False, indent=2) + "\n", key, True
