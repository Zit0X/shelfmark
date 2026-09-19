import json

import requests

from shelfmark.core.cache import get_metadata_cache
from shelfmark.metadata_providers import MetadataSearchOptions
from shelfmark.metadata_providers.babelio import BabelioSearchProvider

BOOK_URL = (
    "https://booknode.com/"
    "les_mysteres_de_little_bramble_tome_1_meurtre_au_festival_des_citrouilles_03723324"
)

QUICKSEARCH_RESPONSE = {
    "search": "Meurtre au festival des citrouilles",
    "book": [
        {
            "idbook": 3723324,
            "name": (
                "Les mystères de Little Bramble, Tome 1 : "
                "Meurtre au Festival des Citrouilles"
            ),
            "href": BOOK_URL,
            "img": (
                "https://cdn1.booknode.com/book_cover/5996/mod11/"
                "les_mysteres_de_little_bramble_tome_1_meurtre_au_festival_des_citrouilles"
                "-5996437-66-108.jpg"
            ),
            "authors": [
                {
                    "idauteur": 1006444,
                    "nom": "Ava Manceau",
                    "_prenom": "Ava",
                    "_nom": "Manceau",
                    "href": "https://booknode.com/auteur/ava-manceau",
                }
            ],
        }
    ],
    "author": [],
    "user": [],
    "elapsed": 0.06265,
}

EMPTY_QUICKSEARCH_RESPONSE = {"search": "", "book": [], "author": [], "user": [], "elapsed": 0.01}

BOOK_HTML = """
<html><body>
<h1>Les mystères de Little Bramble, Tome 1 : Meurtre au Festival des Citrouilles</h1>
<div class="main-cover physical-cover">
    <img class="max_width" src="https://cdn1.booknode.com/book_cover/5996/
les_mysteres_de_little_bramble_tome_1_meurtre_au_festival_des_citrouilles-5996437-264-432.jpg">
</div>
<div class="main-bloc-right">
    <h4>Auteur</h4>
    <a href="https://booknode.com/auteur/ava-manceau"><span>Ava Manceau</span></a>
</div>
<div class="main-bloc-right">
    <h4>Série</h4>
    <a title="Série Les Mystères de Little Bramble"
       href="https://booknode.com/serie/les-mysteres-de-little-bramble">
        Les Mystères de Little Bramble (2 livres)
    </a>
</div>
<div class="main-bloc-right">
    <h4>Thèmes</h4>
    <a href="https://booknode.com/theme/romance_45">Romance</a>,
    <a href="https://booknode.com/theme/humour_422">Humour</a>,
    <a href="https://booknode.com/theme/citrouille_4186953">Citrouille</a>,
    <a href="https://booknode.com/theme/cosy-mystery_4595798">Cosy mystery</a>,
    <a href="https://booknode.com/theme/secrets_4598405">Secrets</a>
</div>
<div class="panel-body">
    <div class="text-body js-readableMore usercomment">
        <span class="actual-text"><p><span class="resume-title">Résumé</span></p>
        <p>À vingt-neuf ans, Poppy Fairchild a décidé de tout quitter.</p>
        <p></p>
        <p>Bienvenue à Little Bramble, où Poppy vient d'ouvrir sa librairie
        Fairchild &amp; Fables, juste à temps pour le Festival des Citrouilles.</p>
        <p></p>
        <p>Le président du festival est retrouvé mort. Poppy n'a aucune intention
        de jouer les détectives. Absolument aucune.</p></span>
    </div>
</div>
</body></html>
"""

BABELIO_CHALLENGE_HTML = """
<!DOCTYPE html><html lang="fr"><head><title>Vérification de sécurité</title></head>
<body><div class="captcha_container">Slider verification required</div></body></html>
"""


class _FakeResponse:
    def __init__(self, *, json_data=None, text=None):
        self._json_data = json_data
        self.text = text if text is not None else (json.dumps(json_data) if json_data else "")

    def raise_for_status(self):
        return None

    def json(self):
        if self._json_data is None:
            raise ValueError("no JSON body")
        return self._json_data


class _FakeSession:
    """Routes requests to canned responses by URL substring."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params))
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        raise requests.HTTPError(f"unexpected URL: {url}")


def _provider(session):
    provider = BabelioSearchProvider()
    provider.session = session
    return provider


class TestBabelioSearchBooknodePrimary:
    """The Ava Manceau cozy mystery this feature exists for: not on Google Books /
    Open Library, but findable through Booknode - Babelio's own fallback should not
    even be consulted since Booknode already has a match.
    """

    def test_search_returns_fully_populated_metadata(self):
        get_metadata_cache().clear()
        session = _FakeSession(
            {"ajax_quicksearch.php": _FakeResponse(json_data=QUICKSEARCH_RESPONSE)}
        )
        provider = _provider(session)

        books = provider.search(
            MetadataSearchOptions(
                query="Meurtre au festival des citrouilles",
                fields={"title": "Meurtre au festival des citrouilles", "author": "Ava Manceau"},
            )
        )

        assert len(books) == 1
        book = books[0]

        assert book.provider == "babelio"
        assert book.provider_id == (
            "bn:les_mysteres_de_little_bramble_tome_1_meurtre_au_festival_des_citrouilles_03723324"
        )
        assert book.title == "Meurtre au Festival des Citrouilles"
        assert book.authors == ["Ava Manceau"]
        assert book.series_name == "Les mystères de Little Bramble"
        assert book.series_position == 1.0
        assert book.cover_url.endswith("-5996437-66-108.jpg")
        assert book.source_url == BOOK_URL
        assert book.language == "fr"
        assert book.search_title == "Meurtre au Festival des Citrouilles"
        assert book.search_author == "Ava Manceau"
        labels = {f.label: f.value for f in book.display_fields}
        assert labels["Series"] == "Les mystères de Little Bramble 1"

        # Babelio must not have been queried at all: Booknode already answered.
        assert all("babelio.com" not in url for url, _ in session.calls)

    def test_search_falls_back_to_babelio_when_booknode_has_no_match(self):
        get_metadata_cache().clear()
        session = _FakeSession(
            {
                "ajax_quicksearch.php": _FakeResponse(json_data=EMPTY_QUICKSEARCH_RESPONSE),
                "babelio.com/resrecherche.php": _FakeResponse(text=BABELIO_CHALLENGE_HTML),
            }
        )
        provider = _provider(session)

        books = provider.search(MetadataSearchOptions(query="Un livre totalement introuvable"))

        # Babelio's anti-bot wall means the fallback degrades to "no results"
        # instead of raising or returning garbage.
        assert books == []
        assert any("babelio.com" in url for url, _ in session.calls)


class TestBabelioGetBook:
    def test_get_book_parses_full_details(self):
        get_metadata_cache().clear()
        session = _FakeSession({"les_mysteres_de_little_bramble": _FakeResponse(text=BOOK_HTML)})
        provider = _provider(session)

        book = provider.get_book(
            "bn:les_mysteres_de_little_bramble_tome_1_meurtre_au_festival_des_citrouilles_03723324"
        )

        assert book is not None
        assert book.title == "Meurtre au Festival des Citrouilles"
        assert book.authors == ["Ava Manceau"]
        assert book.series_name == "Les mystères de Little Bramble"
        assert book.series_position == 1.0
        assert book.series_id == "serie/les-mysteres-de-little-bramble"
        assert "Cosy mystery" in book.genres
        assert book.cover_url.endswith("-5996437-264-432.jpg")
        assert book.language == "fr"

        # Official blurb, extracted from the pinned "Résumé" comment; label stripped.
        assert book.description is not None
        assert "Résumé" not in book.description
        assert "Poppy Fairchild" in book.description
        assert "Festival des Citrouilles" in book.description

    def test_get_book_unknown_prefix_returns_none(self):
        provider = _provider(_FakeSession({}))
        assert provider.get_book("nope:123") is None
