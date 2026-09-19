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

AUTHOR_URL = "https://booknode.com/auteur/ava-manceau"

# Real (trimmed) shape of what Booknode's quicksearch returns for the author-only query
# "Ava Manceau": every "book" hit is fuzzy-matched on the word "Ava" alone and belongs to
# an unrelated author, while the real match only shows up in the "author" bucket.
AUTHOR_QUICKSEARCH_RESPONSE = {
    "search": "Ava Manceau",
    "book": [
        {
            "idbook": 3198145,
            "name": "Dear Ava",
            "href": "https://booknode.com/dear_ava_03198145",
            "img": "https://cdn1.booknode.com/book_cover/5438/mod11/dear_ava-5437885-66-108.jpg",
            "authors": [
                {
                    "idauteur": 544663,
                    "nom": "Ilsa Madden-Mills",
                    "href": "https://booknode.com/auteur/ilsa-madden-mills",
                }
            ],
        },
        {
            "idbook": 2589765,
            "name": "Les Yeux d'Ava",
            "href": "https://booknode.com/les_yeux_dava_02589765",
            "img": "https://cdn1.booknode.com/book_cover/1103/mod11/les_yeux_dava-1102735-66-108.jpg",
            "authors": [
                {
                    "idauteur": 321023,
                    "nom": "Wendall Utroi",
                    "href": "https://booknode.com/auteur/wendall-utroi",
                }
            ],
        },
    ],
    "author": [
        {
            "name": "Ava Manceau",
            "href": AUTHOR_URL,
            "img": "https://cdn1.booknode.com/author_picture/5683/mod11/ava-manceau-5682523-30-40.jpg",
        },
        {"name": "Ava Dellaira", "href": "https://booknode.com/auteur/ava-dellaira", "img": ""},
        {"name": "Ava Reid", "href": "https://booknode.com/auteur/ava-reid", "img": ""},
    ],
    "user": [],
    "elapsed": 0.02,
}

AUTHOR_LIVRES_URL = f"{AUTHOR_URL}/livres"
SERIES_URL = "https://booknode.com/serie/les-mysteres-de-little-bramble"

# Trimmed real structure of https://booknode.com/auteur/ava-manceau/livres: the "Toutes
# les séries" section only links each series as a whole (cover art, no per-tome links),
# while "Tous les livres" links her one-shots directly - both sections reuse the same
# ".panel-book-card" widget and are told apart by where the cover link points.
AUTHOR_LIVRES_HTML = f"""
<html><body>
<h4>Toutes les séries de Ava Manceau</h4>
<div class="row">
    <div class="panel panel-default panel-book-card">
        <div class="cover-wrapper">
            <a href="{SERIES_URL}" class="cover">
                <img src="https://cdn1.booknode.com/book_cover/5996/mod11/tome1-5996437-264-432.jpg">
            </a>
        </div>
        <div class="title">
            <a href="{SERIES_URL}">Série Les Mystères de Little Bramble</a>
        </div>
    </div>
</div>
<h4>Tous les livres de Ava Manceau</h4>
<div class="row">
    <div class="panel panel-default panel-book-card">
        <div class="cover-wrapper">
            <a href="https://booknode.com/les_heritiers_de_laube_03646884" class="cover">
                <img data-src="https://cdn1.booknode.com/book_cover/5714/mod11/
les_heritiers_de_laube-5713515-264-432.jpg" class="lazyload">
            </a>
        </div>
        <div class="title">
            <a href="https://booknode.com/les_heritiers_de_laube_03646884">Les Héritiers de l'aube</a>
        </div>
    </div>
    <div class="panel panel-default panel-book-card">
        <div class="cover-wrapper">
            <a href="https://booknode.com/la_chambre_313_03676158" class="cover">
                <img data-src="https://cdn1.booknode.com/book_cover/1/mod11/la_chambre_313-1-264-432.jpg"
                     class="lazyload">
            </a>
        </div>
        <div class="title">
            <a href="https://booknode.com/la_chambre_313_03676158">La chambre 313</a>
        </div>
    </div>
</div>
</body></html>
"""

# Trimmed real structure of a Booknode series page's "La liste des tomes" - this is what
# it takes to recover individual tomes, since the bibliography page above never links to
# them directly.
SERIES_PAGE_HTML = """
<html><body>
<h2>La liste des tomes</h2>
<article class="liste">
    <div class="book col-xs-12">
        <a title="Les mystères de Little Bramble, Tome 1 : Meurtre au Festival des Citrouilles"
           href="https://booknode.com/les_mysteres_de_little_bramble_tome_1_meurtre_au_festival_des_citrouilles_03723324"
           class="main_cover_link">
            <img data-src="https://cdn1.booknode.com/book_cover/5996/mod11/
les_mysteres_de_little_bramble_tome_1_meurtre_au_festival_des_citrouilles-5996437-264-432.jpg">
        </a>
    </div>
    <div class="book col-xs-12">
        <a title="Les mystères de Little Bramble, Tome 2 : Meurtre au Festival des Boules à Neige"
           href="https://booknode.com/les_mysteres_de_little_bramble_tome_2_meurtre_au_festival_des_boules_a_neige_03723325"
           class="main_cover_link">
            <img data-src="https://cdn1.booknode.com/book_cover/5996/mod11/
les_mysteres_de_little_bramble_tome_2_meurtre_au_festival_des_boules_a_neige-5996439-264-432.jpg">
        </a>
    </div>
</article>
<h3>Commentaires</h3>
</body></html>
"""

# A false-positive author suggestion Booknode actually returns for a pure title search
# (fuzzy-matched on the single shared word "des"), which must not hijack the search.
TITLE_SEARCH_WITH_SPURIOUS_AUTHOR_RESPONSE = {
    **QUICKSEARCH_RESPONSE,
    "author": [
        {"name": "Guy Des Cars", "href": "https://booknode.com/auteur/guy-des-cars", "img": ""},
    ],
}

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


class TestBabelioSearchByAuthor:
    """Regression coverage for the "Ava Manceau" bug: Booknode's quicksearch fuzzy-
    matches book titles, so an author-only query used to surface unrelated books
    (Dear Ava, Les Yeux d'Ava...) that merely share a word, and never Ava Manceau's own
    books. The fix must recognize the genuine author match, fetch her full bibliography
    page, and expand every series listed there into its individual tomes.
    """

    def test_author_only_search_returns_her_full_bibliography(self):
        get_metadata_cache().clear()
        session = _FakeSession(
            {
                "ajax_quicksearch.php": _FakeResponse(json_data=AUTHOR_QUICKSEARCH_RESPONSE),
                "auteur/ava-manceau/livres": _FakeResponse(text=AUTHOR_LIVRES_HTML),
                "serie/les-mysteres-de-little-bramble": _FakeResponse(text=SERIES_PAGE_HTML),
            }
        )
        provider = _provider(session)

        books = provider.search(MetadataSearchOptions(query="Ava Manceau"))

        titles = [b.title for b in books]
        # Her 2 one-shots (from the bibliography page) + both tomes of her series
        # (from expanding the series page) - not just the series' first/only card.
        assert len(books) == 4
        assert "Les Héritiers de l'aube" in titles
        assert "La chambre 313" in titles
        assert "Meurtre au Festival des Citrouilles" in titles
        assert "Meurtre au Festival des Boules à Neige" in titles
        # None of the fuzzy "Ava"-titled books by other authors should appear.
        assert "Dear Ava" not in titles
        assert "Les Yeux d'Ava" not in titles
        # The series card itself (a series, not a book) must not be returned as a result.
        assert "Les Mystères de Little Bramble" not in titles
        assert all(not b.provider_id.startswith("bn:serie") for b in books)

        target = next(b for b in books if b.title == "Meurtre au Festival des Citrouilles")
        assert target.authors == ["Ava Manceau"]
        assert target.series_name == "Les mystères de Little Bramble"
        assert target.series_position == 1.0
        assert target.provider == "babelio"
        assert target.provider_id == (
            "bn:les_mysteres_de_little_bramble_tome_1_meurtre_au_festival_des_citrouilles_03723324"
        )

        tome_2 = next(b for b in books if b.title == "Meurtre au Festival des Boules à Neige")
        assert tome_2.series_position == 2.0

    def test_author_search_stops_expanding_series_once_limit_is_reached(self):
        """Fetching every tome of every series is expensive - a caller asking for a
        handful of results should not pay for the whole catalog being crawled.
        """
        get_metadata_cache().clear()
        session = _FakeSession(
            {
                "ajax_quicksearch.php": _FakeResponse(json_data=AUTHOR_QUICKSEARCH_RESPONSE),
                "auteur/ava-manceau/livres": _FakeResponse(text=AUTHOR_LIVRES_HTML),
                "serie/les-mysteres-de-little-bramble": _FakeResponse(text=SERIES_PAGE_HTML),
            }
        )
        provider = _provider(session)

        # The 2 one-shots alone already satisfy this limit; the series page must not
        # even be requested.
        books = provider.search(MetadataSearchOptions(query="Ava Manceau", limit=2))

        assert len(books) == 2
        assert all("serie/" not in url for url, _ in session.calls)

    def test_spurious_author_suggestion_does_not_hijack_a_title_search(self):
        get_metadata_cache().clear()
        session = _FakeSession(
            {
                "ajax_quicksearch.php": _FakeResponse(
                    json_data=TITLE_SEARCH_WITH_SPURIOUS_AUTHOR_RESPONSE
                ),
            }
        )
        provider = _provider(session)

        books = provider.search(MetadataSearchOptions(query="Meurtre au festival des citrouilles"))

        assert len(books) == 1
        assert books[0].title == "Meurtre au Festival des Citrouilles"
        # The unrelated "Guy Des Cars" suggestion must never trigger an author-page fetch.
        assert all("guy-des-cars" not in url for url, _ in session.calls)


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
