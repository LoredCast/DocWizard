"""Adding a reference by DOI, ISBN or arXiv ID (§10.5)."""

import json

import pytest

from docwizard.citations import lookup
from docwizard.netpolicy import Response
from tests.integration.test_collab import new_project, open_editor

NATURE = {"type": "article-journal", "title": "Deep learning", "container-title": "Nature", "volume": "521",
          "issue": "7553", "page": "436-444", "DOI": "10.1038/nature14539", "publisher": "Springer",
          "author": [{"family": "LeCun", "given": "Yann"}, {"family": "Bengio", "given": "Yoshua"}],
          "issued": {"date-parts": [[2015, 5, 27]]}, "reference": [{"key": "x"}], "license": [{"URL": "y"}]}
ARXIV = {"type": "article", "title": "Attention Is All You Need", "DOI": "10.48550/ARXIV.1706.03762",
         "author": [{"family": "Vaswani", "given": "Ashish"}], "issued": {"date-parts": [[2017]]}}
BOOK = {"ISBN:9780262033848": {"title": "Introduction to Algorithms", "subtitle": "Third Edition",
                               "authors": [{"name": "Thomas H. Cormen"}, {"name": "Leiserson, Charles E."}],
                               "publishers": [{"name": "MIT Press"}], "publish_places": [{"name": "Cambridge, Mass"}],
                               "publish_date": "July 31, 2009", "number_of_pages": 1292}}


def test_identifiers_are_recognized():
    assert lookup.classify("https://doi.org/10.1038/nature14539") == ("doi", "10.1038/nature14539")
    assert lookup.classify("doi: 10.1145/3313831.3376150.") == ("doi", "10.1145/3313831.3376150")
    assert lookup.classify("arXiv:1706.03762v5") == ("doi", "10.48550/arXiv.1706.03762")
    assert lookup.classify("https://arxiv.org/abs/hep-th/9901001") == ("doi", "10.48550/arXiv.hep-th/9901001")
    assert lookup.classify("ISBN 978-0-262-03384-8") == ("isbn", "9780262033848")
    assert lookup.classify("0-262-03384-4") == ("isbn", "0262033844")
    for bad in ("978-0-262-03384-9", "hello", "10.12/x"):
        with pytest.raises(lookup.IdentifierError):
            lookup.classify(bad)


def test_keys_and_files():
    item = lookup._clean(NATURE)
    assert "reference" not in item and "license" not in item
    text, key, added = lookup.add_to_file(None, item)
    assert key == "lecun2015deep" and added
    text, key2, added = lookup.add_to_file(text, dict(item, DOI="10.1/other"))
    assert key2 == "lecun2015deepa" and added
    again, key3, added = lookup.add_to_file(text, item)
    assert key3 == "lecun2015deep" and not added and again == text
    assert [i["id"] for i in json.loads(text)] == ["lecun2015deep", "lecun2015deepa"]
    book = lookup._book(BOOK["ISBN:9780262033848"], "9780262033848")
    assert book["title"] == "Introduction to Algorithms: Third Edition" and book["issued"] == {"date-parts": [[2009]]}
    assert book["author"] == [{"family": "Cormen", "given": "Thomas H."}, {"family": "Leiserson", "given": "Charles E."}]
    assert lookup.make_key(book, set()) == "cormen2009introduction"
    with pytest.raises(lookup.IdentifierError):
        lookup.add_to_file("{not json", item)


async def test_add_reference_api(client, app, monkeypatch):
    calls = []

    def fake_fetch(method, url, headers=None, **kw):
        calls.append((url, headers.get("Accept")))
        if "openlibrary.org" in url:
            return Response(200, {}, json.dumps(BOOK).encode(), url)
        if url.endswith("nature14539"):
            return Response(200, {}, json.dumps(NATURE).encode(), url)
        if "arXiv.1706.03762" in url:
            return Response(200, {}, json.dumps(ARXIV).encode(), url)
        return Response(404, {}, b"", url)

    from docwizard.netpolicy import Policy

    monkeypatch.setattr(Policy, "fetch", lambda self, *a, **kw: fake_fetch(*a, **kw))
    pid = await new_project(client)
    r = await client.post(f"/api/projects/{pid}/references", json={"id": "10.1038/nature14539"})
    assert r.status == 201, r.text
    assert r.json()["key"] == "lecun2015deep" and r.json()["file"] == "references.json"
    assert calls[0] == ("https://doi.org/10.1038/nature14539", "application/vnd.citationstyles.csl+json")
    r = await client.post(f"/api/projects/{pid}/references", json={"id": "arXiv:1706.03762"})
    assert r.json()["key"] == "vaswani2017attention" and r.json()["item"]["number"] == "arXiv:1706.03762"
    r = await client.post(f"/api/projects/{pid}/references", json={"id": "978-0-262-03384-8"})
    assert r.json()["key"] == "cormen2009introduction"
    # Again: reused, not duplicated.
    r = await client.post(f"/api/projects/{pid}/references", json={"id": "https://doi.org/10.1038/nature14539"})
    assert r.status == 200 and not r.json()["added"]
    snap = await open_editor(client, pid, "references.json")
    # The starter project's references.json keeps its own entry; the new ones follow.
    assert [i["id"] for i in json.loads(snap["text"])][1:] == ["lecun2015deep", "vaswani2017attention",
                                                         "cormen2009introduction"]
    r = await client.post(f"/api/projects/{pid}/references", json={"id": "10.9999/missing"})
    assert r.status == 422 and "No record" in r.json()["error"]["message"]
    r = await client.post(f"/api/projects/{pid}/references", json={"id": "nonsense"})
    assert r.status == 422
    r = await client.post(f"/api/projects/{pid}/references", json={"id": "10.1038/nature14539", "file": "x.bib"})
    assert r.status == 400
