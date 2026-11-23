import json

import pytest
import requests

from shelfmark.core.cache import get_metadata_cache
from shelfmark.metadata_providers import (
    MetadataSearchOptions,
    SearchType,
    get_provider,
    is_provider_registered,
)
from shelfmark.metadata_providers import babelio as babelio_module
from shelfmark.metadata_providers.babelio import BabelioProvider
from shelfmark.metadata_providers.booknode import BooknodeProvider

# What Babelio actually serves to non-browser clients (HTTP 403).
CHALLENGE_HTML = """
<!DOCTYPE html><html lang="fr"><head><title>Vérification de sécurité</title></head>
<body><div class="captcha_container">Slider verification required</div></body></html>
"""

# Babelio's result markup is captcha-gated, so this only relies on /livres/<slug>/<id> links.
SEARCH_HTML = """
<html><body>
<div class="cr_meta">
  <a href="/livres/Manceau-Meurtre-au-festival-des-citrouilles/1612345">
    Meurtre au festival des citrouilles</a>
  <a href="/auteur/Ava-Manceau/712345">Ava Manceau</a>
</div>
<div class="cr_meta">
  <a href="/livres/Manceau-Meurtre-au-festival-des-citrouilles/1612345"><img src="x.jpg"></a>
  <a href="https://www.babelio.com/livres/Manceau-Meurtre-au-festival-des-boules-a-neige/1623456">
    Meurtre au festival des boules à neige</a>
</div>
</body></html>
"""

BOOK_HTML = """
<html><head>
<meta property="og:title" content="Meurtre au festival des citrouilles - Ava Manceau - Babelio">
<meta property="og:image" content="https://www.babelio.com/couv/CVT_Meurtre_1234.jpg">
<meta property="og:description" content="Poppy Fairchild ouvre sa librairie.">
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Book", "name": "Meurtre au festival des citrouilles",
 "author": {"@type": "Person", "name": "Ava Manceau"}, "isbn": "979-10-415-1234-5"}
</script>
</head><body><h1>Meurtre au festival des citrouilles</h1></body></html>
"""

BOOKNODE_QUICKSEARCH = {
    "book": [
        {
            "name": "Les mystères de Little Bramble, Tome 1 : Meurtre au Festival des Citrouilles",
            "href": "https://booknode.com/les_mysteres_de_little_bramble_tome_1_03723324",
            "img": "",
            "authors": [{"nom": "Ava Manceau", "href": "https://booknode.com/auteur/ava-manceau"}],
        }
    ],
    "author": [],
}


class _FakeResponse:
    def __init__(self, *, text="", json_data=None, status_code=200):
        self.text = text if json_data is None else json.dumps(json_data)
        self._json_data = json_data
        self.status_code = status_code
        self.ok = status_code < 400

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._json_data is None:
            msg = "no JSON body"
            raise ValueError(msg)
        return self._json_data


class _FakeSession:
    def __init__(self, routes, calls):
        self.routes = routes
        self.calls = calls
        self.headers = {}

    def get(self, url, params=None, **_kwargs):
        self.calls.append((url, params))
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        raise requests.ConnectionError(f"unexpected URL: {url}")


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    get_metadata_cache().clear()
    monkeypatch.setattr(babelio_module, "_blocked_until", 0.0)
    monkeypatch.setattr(babelio_module, "_booknode_fallback_enabled", lambda: True)


def _provider(routes):
    """Babelio provider whose Babelio and Booknode sessions share one call log."""
    calls = []
    provider = BabelioProvider()
    provider.session = _FakeSession(routes, calls)
    booknode = BooknodeProvider()
    booknode.session = _FakeSession(routes, calls)
    provider._booknode = booknode
    return provider, calls


def test_provider_is_registered():
    assert is_provider_registered("babelio")
    assert isinstance(get_provider("babelio"), BabelioProvider)


class TestSearch:
    def test_parses_babelio_results(self):
        provider, calls = _provider({"resrecherche.php": _FakeResponse(text=SEARCH_HTML)})

        books = provider.search(MetadataSearchOptions(query="Meurtre au festival"))

        assert [(b.provider_id, b.title) for b in books] == [
            (
                "1612345:Manceau-Meurtre-au-festival-des-citrouilles",
                "Meurtre au festival des citrouilles",
            ),
            (
                "1623456:Manceau-Meurtre-au-festival-des-boules-a-neige",
                "Meurtre au festival des boules à neige",
            ),
        ]
        assert books[0].provider == "babelio"
        assert books[0].source_url == (
            "https://www.babelio.com/livres/Manceau-Meurtre-au-festival-des-citrouilles/1612345"
        )
        assert not any("booknode" in url for url, _ in calls)

    def test_captcha_falls_back_to_booknode(self):
        provider, _ = _provider(
            {
                "resrecherche.php": _FakeResponse(text=CHALLENGE_HTML, status_code=403),
                "ajax_quicksearch.php": _FakeResponse(json_data=BOOKNODE_QUICKSEARCH),
            }
        )

        books = provider.search(MetadataSearchOptions(query="Meurtre au festival"))

        assert [b.title for b in books] == ["Meurtre au Festival des Citrouilles"]
        # Booknode books keep their own provider so their details resolve through it.
        assert books[0].provider == "booknode"

    def test_captcha_starts_a_backoff(self):
        provider, calls = _provider(
            {
                "resrecherche.php": _FakeResponse(text=CHALLENGE_HTML, status_code=403),
                "ajax_quicksearch.php": _FakeResponse(json_data=BOOKNODE_QUICKSEARCH),
            }
        )

        provider.search(MetadataSearchOptions(query="premier"))
        provider.search(MetadataSearchOptions(query="second"))

        assert sum("babelio.com" in url for url, _ in calls) == 1
        assert sum("booknode.com" in url for url, _ in calls) == 2

    def test_challenge_page_with_200_status_is_detected(self):
        provider, _ = _provider(
            {
                "resrecherche.php": _FakeResponse(text=CHALLENGE_HTML),
                "ajax_quicksearch.php": _FakeResponse(json_data=BOOKNODE_QUICKSEARCH),
            }
        )
        books = provider.search(MetadataSearchOptions(query="test"))
        assert [b.provider for b in books] == ["booknode"]

    def test_no_fallback_when_disabled(self, monkeypatch):
        monkeypatch.setattr(babelio_module, "_booknode_fallback_enabled", lambda: False)
        provider, calls = _provider(
            {"resrecherche.php": _FakeResponse(text=CHALLENGE_HTML, status_code=403)}
        )

        assert provider.search(MetadataSearchOptions(query="test")) == []
        assert not any("booknode" in url for url, _ in calls)

    def test_later_pages_of_babelio_results_do_not_fall_back(self):
        provider, calls = _provider({"resrecherche.php": _FakeResponse(text=SEARCH_HTML)})

        result = provider.search_paginated(MetadataSearchOptions(query="test", limit=2, page=2))

        assert result.books == []
        assert result.has_more is False
        assert not any("booknode" in url for url, _ in calls)

    def test_isbn_search_fetches_the_book(self):
        provider, _ = _provider(
            {
                "resrecherche.php": _FakeResponse(text=SEARCH_HTML),
                "/livres/": _FakeResponse(text=BOOK_HTML),
            }
        )

        books = provider.search(
            MetadataSearchOptions(query="979-10-415-1234-5", search_type=SearchType.ISBN)
        )

        assert len(books) == 1
        assert books[0].isbn_13 == "9791041512345"


class TestGetBook:
    def test_parses_json_ld_and_opengraph(self):
        provider, calls = _provider({"/livres/": _FakeResponse(text=BOOK_HTML)})

        book = provider.get_book("1612345:Manceau-Meurtre-au-festival-des-citrouilles")

        assert book is not None
        assert calls[0][0] == (
            "https://www.babelio.com/livres/Manceau-Meurtre-au-festival-des-citrouilles/1612345"
        )
        assert book.title == "Meurtre au festival des citrouilles"
        assert book.authors == ["Ava Manceau"]
        assert book.isbn_13 == "9791041512345"
        assert book.cover_url == "https://www.babelio.com/couv/CVT_Meurtre_1234.jpg"
        assert book.description == "Poppy Fairchild ouvre sa librairie."

    def test_captcha_returns_none(self):
        provider, _ = _provider({"/livres/": _FakeResponse(text=CHALLENGE_HTML, status_code=403)})
        assert provider.get_book("1612345:Some-Book") is None

    def test_legacy_booknode_id_is_served_by_booknode(self):
        provider, calls = _provider(
            {"booknode.com/la_chambre_313": _FakeResponse(text="<h1>La chambre 313</h1>")}
        )

        book = provider.get_book("bn:la_chambre_313_03676158")

        assert book is not None
        assert book.provider == "booknode"
        assert book.title == "La chambre 313"
        assert calls[0][0] == "https://booknode.com/la_chambre_313_03676158"

    @pytest.mark.parametrize("book_id", ["bab:1612345", "1612345", "x:../etc", ""])
    def test_unusable_ids_make_no_request(self, book_id):
        provider, calls = _provider({})
        assert provider.get_book(book_id) is None
        assert calls == []
