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
from shelfmark.metadata_providers.booknode import (
    BooknodeProvider,
    _upscale_cover,
    is_confident_author_match,
    split_series_title,
)

# Shortened copies of real Booknode responses and pages.
TOME_1_SLUG = "les_mysteres_de_little_bramble_tome_1_meurtre_au_festival_des_citrouilles_03723324"
TOME_1_URL = f"https://booknode.com/{TOME_1_SLUG}"
AUTHOR_URL = "https://booknode.com/auteur/ava-manceau"
SERIES_URL = "https://booknode.com/serie/les-mysteres-de-little-bramble"

QUICKSEARCH_RESPONSE = {
    "search": "Meurtre au festival des citrouilles",
    "book": [
        {
            "idbook": 3723324,
            "name": (
                "Les mystères de Little Bramble, Tome 1 : Meurtre au Festival des Citrouilles"
            ),
            "href": TOME_1_URL,
            "img": (
                "https://cdn1.booknode.com/book_cover/5996/mod11/"
                "les_mysteres_de_little_bramble_tome_1_meurtre_au_festival_des_citrouilles"
                "-5996437-30-40.webp"
            ),
            "authors": [{"idauteur": 1006444, "nom": "Ava Manceau", "href": AUTHOR_URL}],
        }
    ],
    "author": [],
    "user": [],
}

EMPTY_QUICKSEARCH_RESPONSE = {"search": "", "book": [], "author": [], "user": []}

# For the author-only query "Ava Manceau" every "book" hit is fuzzy-matched on "Ava"
# and belongs to someone else; the real match is only in the "author" bucket.
AUTHOR_QUICKSEARCH_RESPONSE = {
    "search": "Ava Manceau",
    "book": [
        {
            "name": "Dear Ava",
            "href": "https://booknode.com/dear_ava_03198145",
            "img": "",
            "authors": [
                {
                    "nom": "Ilsa Madden-Mills",
                    "href": "https://booknode.com/auteur/ilsa-madden-mills",
                }
            ],
        },
        {
            "name": "Les Yeux d'Ava",
            "href": "https://booknode.com/les_yeux_dava_02589765",
            "img": "",
            "authors": [
                {"nom": "Wendall Utroi", "href": "https://booknode.com/auteur/wendall-utroi"}
            ],
        },
    ],
    "author": [
        {"name": "Ava Manceau", "href": AUTHOR_URL, "img": ""},
        {"name": "Ava Dellaira", "href": "https://booknode.com/auteur/ava-dellaira", "img": ""},
    ],
    "user": [],
}

# A pure title search that Booknode answers with an unrelated author suggestion
# (matched on the single shared word "des").
SPURIOUS_AUTHOR_RESPONSE = {
    **QUICKSEARCH_RESPONSE,
    "author": [{"name": "Guy Des Cars", "href": "https://booknode.com/auteur/guy-des-cars"}],
}

# /auteur/ava-manceau/livres: series are linked as a whole, one-shots directly; both
# use the same ".panel-book-card" widget.
AUTHOR_LIVRES_HTML = f"""
<html><body>
<h4>Toutes les séries de Ava Manceau</h4>
<div class="panel panel-default panel-book-card">
    <div class="cover-wrapper">
        <a href="{SERIES_URL}" class="cover">
            <img src="https://cdn1.booknode.com/version/v2/global/img/transparent-22x36.png">
        </a>
    </div>
    <div class="title"><a href="{SERIES_URL}">Série Les Mystères de Little Bramble</a></div>
</div>
<h4>Tous les livres de Ava Manceau</h4>
<div class="panel panel-default panel-book-card">
    <div class="cover-wrapper">
        <a href="https://booknode.com/les_heritiers_de_laube_03646884" class="cover">
            <img data-src="https://cdn1.booknode.com/book_cover/5714/mod11/les_heritiers_de_laube-5713515-264-432.jpg"
                 class="lazyload">
        </a>
    </div>
    <div class="title">
        <a href="https://booknode.com/les_heritiers_de_laube_03646884">Les Héritiers de l'aube</a>
    </div>
</div>
<div class="panel panel-default panel-book-card">
    <div class="cover-wrapper">
        <a href="https://booknode.com/la_chambre_313_03676158" class="cover">
            <img data-src="https://cdn1.booknode.com/book_cover/1/mod11/la_chambre_313-1-264-432.jpg">
        </a>
    </div>
    <div class="title"><a href="https://booknode.com/la_chambre_313_03676158">La chambre 313</a></div>
</div>
</body></html>
"""

SERIES_PAGE_HTML = f"""
<html><body>
<h2>La liste des tomes</h2>
<article class="liste">
    <div class="book col-xs-12">
        <a title="Les mystères de Little Bramble, Tome 1 : Meurtre au Festival des Citrouilles"
           href="{TOME_1_URL}" class="main_cover_link">
            <img data-src="https://cdn1.booknode.com/book_cover/5996/mod11/t1-5996437-264-432.jpg">
        </a>
    </div>
    <div class="book col-xs-12">
        <a title="Les mystères de Little Bramble, Tome 2 : Meurtre au Festival des Boules à Neige"
           href="https://booknode.com/les_mysteres_de_little_bramble_tome_2_meurtre_au_festival_des_boules_a_neige_03723325"
           class="main_cover_link">
            <img data-src="https://cdn1.booknode.com/book_cover/5996/mod11/t2-5996439-264-432.jpg">
        </a>
    </div>
</article>
</body></html>
"""

BOOK_JSON_LD = {
    "@context": "https://schema.org",
    "@type": "Book",
    "name": "Harry Potter, Tome 1 : Harry Potter à l'école des sorciers",
    "inLanguage": "fr",
    "image": "https://cdn1.booknode.com/book_cover/835/full/harry-potter-835229.jpg",
    "author": [
        {
            "@type": "Person",
            "name": "Joanne Kathleen Rowling",
            "url": "https://booknode.com/auteur/joanne-kathleen-rowling",
        }
    ],
    "description": "Le jour de ses onze ans, Harry Potter, un orphelin, l&#039;apprend.",
    "datePublished": "2011-11-07",
    "genre": ["Aventure", "Fantastique", "Magie"],
    "isbn": "9782075187541",
    "aggregateRating": {
        "@type": "AggregateRating",
        "ratingValue": 8.81,
        "ratingCount": 56099,
        "bestRating": 10,
        "worstRating": 1,
    },
    "isPartOf": {
        "@type": "BookSeries",
        "name": "Harry Potter",
        "url": "https://booknode.com/serie/harry-potter",
        "position": 1,
    },
}

BOOK_HTML = f"""
<html><head>
<script type="application/ld+json">{json.dumps(BOOK_JSON_LD)}</script>
</head><body>
<h1>Harry Potter, Tome 1 : Harry Potter à l'école des sorciers</h1>
<div class="main-cover physical-cover">
    <img src="https://cdn1.booknode.com/book_cover/835/harry_potter_tome_1-835229-264-432.jpg">
</div>
<a href="/editeur/gallimard-jeunesse-2" title="Editeur Gallimard Jeunesse">Gallimard Jeunesse</a>
</body></html>
"""

# A page without JSON-LD: everything comes from the HTML.
BOOK_HTML_NO_JSON_LD = """
<html><body>
<h1>Les mystères de Little Bramble, Tome 1 : Meurtre au Festival des Citrouilles</h1>
<div class="main-cover physical-cover">
    <img src="https://cdn1.booknode.com/book_cover/5996/t1-5996437-264-432.jpg">
</div>
<a href="https://booknode.com/auteur/ava-manceau"><span>Ava Manceau</span></a>
<a href="https://booknode.com/serie/les-mysteres-de-little-bramble">Les Mystères de Little Bramble</a>
<a href="https://booknode.com/theme/romance_45">Romance</a>,
<a href="https://booknode.com/theme/cosy-mystery_4595798">Cosy mystery</a>
<div class="text-body usercomment">
    <span class="actual-text"><p><span class="resume-title">Résumé</span></p>
    <p>À vingt-neuf ans, Poppy Fairchild a décidé de tout quitter.</p>
    <p></p>
    <p>Bienvenue à Little Bramble.</p></span>
</div>
</body></html>
"""


class _FakeResponse:
    def __init__(self, *, json_data=None, text=None, status_code=200):
        self._json_data = json_data
        self.text = text if text is not None else json.dumps(json_data)
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
    """Answer requests with canned responses, picked by URL substring."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, **_kwargs):
        self.calls.append((url, params))
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        raise requests.ConnectionError(f"unexpected URL: {url}")


@pytest.fixture(autouse=True)
def _clear_cache():
    get_metadata_cache().clear()


def _provider(routes):
    provider = BooknodeProvider()
    provider.session = _FakeSession(routes)
    return provider


def _urls(provider):
    return [url for url, _ in provider.session.calls]


def test_provider_is_registered():
    assert is_provider_registered("booknode")
    assert isinstance(get_provider("booknode"), BooknodeProvider)


class TestHelpers:
    def test_split_series_title(self):
        assert split_series_title("Harry Potter, Tome 1 : À l'école des sorciers") == (
            "Harry Potter",
            1.0,
            "À l'école des sorciers",
        )
        assert split_series_title("Saga, Tome 0,5 : Prélude") == ("Saga", 0.5, "Prélude")
        assert split_series_title("La chambre 313") == (None, None, "La chambre 313")

    def test_upscale_cover(self):
        assert _upscale_cover(
            "https://cdn1.booknode.com/book_cover/835/mod11/hp_tome_1-835229-30-40.webp"
        ) == ("https://cdn1.booknode.com/book_cover/835/hp_tome_1-835229-264-432.jpg")
        assert _upscale_cover("") is None
        assert _upscale_cover(None) is None

    def test_confident_author_match(self):
        assert is_confident_author_match("Ava Manceau", "Ava Manceau")
        assert is_confident_author_match("Ava Manceau", "manceau")
        assert is_confident_author_match("Joanne Kathleen Rowling", "Rowling")
        assert not is_confident_author_match("Guy Des Cars", "Meurtre au festival des citrouilles")


class TestSearch:
    def test_title_search_returns_populated_metadata(self):
        provider = _provider(
            {"ajax_quicksearch.php": _FakeResponse(json_data=QUICKSEARCH_RESPONSE)}
        )

        books = provider.search(
            MetadataSearchOptions(
                query="",
                fields={"title": "Meurtre au festival des citrouilles", "author": "Ava Manceau"},
            )
        )

        assert len(books) == 1
        book = books[0]
        assert book.provider == "booknode"
        assert book.provider_id == TOME_1_SLUG
        assert book.provider_display_name == "Booknode"
        assert book.title == "Meurtre au Festival des Citrouilles"
        assert book.authors == ["Ava Manceau"]
        assert book.series_name == "Les mystères de Little Bramble"
        assert book.series_position == 1.0
        assert book.cover_url is not None
        assert book.cover_url.endswith("-5996437-264-432.jpg")
        assert "/mod11/" not in book.cover_url
        assert book.source_url == TOME_1_URL
        assert book.language == "fr"
        assert book.search_author == "Ava Manceau"
        assert {f.label: f.value for f in book.display_fields} == {
            "Series": "Les mystères de Little Bramble 1"
        }
        assert provider.session.calls[0][1] == {
            "search": "Meurtre au festival des citrouilles Ava Manceau"
        }

    def test_spurious_author_suggestion_does_not_hijack_a_title_search(self):
        provider = _provider(
            {"ajax_quicksearch.php": _FakeResponse(json_data=SPURIOUS_AUTHOR_RESPONSE)}
        )

        books = provider.search(MetadataSearchOptions(query="Meurtre au festival des citrouilles"))

        assert [b.title for b in books] == ["Meurtre au Festival des Citrouilles"]
        assert not any("guy-des-cars" in url for url in _urls(provider))

    def test_author_query_returns_full_bibliography_with_series_expanded(self):
        provider = _provider(
            {
                "ajax_quicksearch.php": _FakeResponse(json_data=AUTHOR_QUICKSEARCH_RESPONSE),
                "auteur/ava-manceau/livres": _FakeResponse(text=AUTHOR_LIVRES_HTML),
                "serie/les-mysteres-de-little-bramble": _FakeResponse(text=SERIES_PAGE_HTML),
            }
        )

        books = provider.search(MetadataSearchOptions(query="Ava Manceau"))

        titles = [b.title for b in books]
        assert titles == [
            "Les Héritiers de l'aube",
            "La chambre 313",
            "Meurtre au Festival des Citrouilles",
            "Meurtre au Festival des Boules à Neige",
        ]
        assert all(b.authors == ["Ava Manceau"] for b in books)
        tome_2 = books[3]
        assert tome_2.series_name == "Les mystères de Little Bramble"
        assert tome_2.series_position == 2.0
        assert books[0].cover_url == (
            "https://cdn1.booknode.com/book_cover/5714/les_heritiers_de_laube-5713515-264-432.jpg"
        )

    def test_author_field_search_reads_bibliography_even_when_quicksearch_has_books(self):
        response = {**AUTHOR_QUICKSEARCH_RESPONSE, "book": QUICKSEARCH_RESPONSE["book"]}
        provider = _provider(
            {
                "ajax_quicksearch.php": _FakeResponse(json_data=response),
                "auteur/ava-manceau/livres": _FakeResponse(text=AUTHOR_LIVRES_HTML),
                "serie/les-mysteres-de-little-bramble": _FakeResponse(text=SERIES_PAGE_HTML),
            }
        )

        books = provider.search(MetadataSearchOptions(query="", fields={"author": "Ava Manceau"}))

        assert len(books) == 4

    def test_author_search_type_uses_query_as_author(self):
        provider = _provider(
            {
                "ajax_quicksearch.php": _FakeResponse(json_data=AUTHOR_QUICKSEARCH_RESPONSE),
                "auteur/ava-manceau/livres": _FakeResponse(text=AUTHOR_LIVRES_HTML),
                "serie/les-mysteres-de-little-bramble": _FakeResponse(text=SERIES_PAGE_HTML),
            }
        )

        books = provider.search(
            MetadataSearchOptions(query="Ava Manceau", search_type=SearchType.AUTHOR)
        )

        assert len(books) == 4

    def test_series_are_only_expanded_until_the_page_is_full(self):
        provider = _provider(
            {
                "ajax_quicksearch.php": _FakeResponse(json_data=AUTHOR_QUICKSEARCH_RESPONSE),
                "auteur/ava-manceau/livres": _FakeResponse(text=AUTHOR_LIVRES_HTML),
                "serie/les-mysteres-de-little-bramble": _FakeResponse(text=SERIES_PAGE_HTML),
            }
        )

        # 1 per page + 1 look-ahead = the 2 one-shots: the series page is not needed.
        result = provider.search_paginated(MetadataSearchOptions(query="Ava Manceau", limit=1))

        assert [b.title for b in result.books] == ["Les Héritiers de l'aube"]
        assert result.has_more is True
        assert not any("/serie/" in url for url in _urls(provider))

    def test_pagination_slices_without_repeating_books(self):
        provider = _provider(
            {
                "ajax_quicksearch.php": _FakeResponse(json_data=AUTHOR_QUICKSEARCH_RESPONSE),
                "auteur/ava-manceau/livres": _FakeResponse(text=AUTHOR_LIVRES_HTML),
                "serie/les-mysteres-de-little-bramble": _FakeResponse(text=SERIES_PAGE_HTML),
            }
        )

        page_1 = provider.search_paginated(MetadataSearchOptions(query="Ava Manceau", limit=3))
        page_2 = provider.search_paginated(
            MetadataSearchOptions(query="Ava Manceau", limit=3, page=2)
        )

        assert len(page_1.books) == 3
        assert page_1.has_more is True
        assert [b.title for b in page_2.books] == ["Meurtre au Festival des Boules à Neige"]
        assert page_2.has_more is False

    def test_empty_results(self):
        provider = _provider(
            {"ajax_quicksearch.php": _FakeResponse(json_data=EMPTY_QUICKSEARCH_RESPONSE)}
        )
        result = provider.search_paginated(MetadataSearchOptions(query="introuvable"))
        assert result.books == []
        assert result.has_more is False

    def test_fetch_failure_is_not_cached(self):
        provider = _provider({"ajax_quicksearch.php": _FakeResponse(text="<html>", json_data=None)})
        assert provider.search(MetadataSearchOptions(query="test")) == []

        provider.session.routes["ajax_quicksearch.php"] = _FakeResponse(
            json_data=QUICKSEARCH_RESPONSE
        )
        assert len(provider.search(MetadataSearchOptions(query="test"))) == 1

    def test_isbn_search_makes_no_request(self):
        provider = _provider({})
        books = provider.search(
            MetadataSearchOptions(query="9782075187541", search_type=SearchType.ISBN)
        )
        assert books == []
        assert provider.session.calls == []


class TestGetBook:
    def test_parses_json_ld(self):
        provider = _provider({"harry_potter": _FakeResponse(text=BOOK_HTML)})

        book = provider.get_book("harry_potter_tome_1_harry_potter_a_lecole_des_sorciers_0983")

        assert book is not None
        assert book.provider_id == "harry_potter_tome_1_harry_potter_a_lecole_des_sorciers_0983"
        assert book.title == "Harry Potter à l'école des sorciers"
        assert book.authors == ["Joanne Kathleen Rowling"]
        assert book.series_name == "Harry Potter"
        assert book.series_position == 1.0
        assert book.series_id == "serie/harry-potter"
        assert book.isbn_13 == "9782075187541"
        assert book.publish_year == 2011
        assert book.publisher == "Gallimard Jeunesse"
        assert book.genres == ["Aventure", "Fantastique", "Magie"]
        assert book.description == "Le jour de ses onze ans, Harry Potter, un orphelin, l'apprend."
        # The page's 264x432 cover, not JSON-LD's multi-megabyte original.
        assert book.cover_url is not None
        assert book.cover_url.endswith("-835229-264-432.jpg")
        assert {f.label: f.value for f in book.display_fields} == {
            "Rating": "8.81/10",
            "Ratings": "56,099",
        }

    def test_falls_back_to_html_without_json_ld(self):
        provider = _provider({"les_mysteres": _FakeResponse(text=BOOK_HTML_NO_JSON_LD)})

        book = provider.get_book(TOME_1_SLUG)

        assert book is not None
        assert book.title == "Meurtre au Festival des Citrouilles"
        assert book.authors == ["Ava Manceau"]
        assert book.series_name == "Les mystères de Little Bramble"
        assert book.series_position == 1.0
        assert book.series_id == "serie/les-mysteres-de-little-bramble"
        assert book.genres == ["Romance", "Cosy mystery"]
        assert book.description is not None
        assert "Résumé" not in book.description
        assert book.description.startswith("À vingt-neuf ans, Poppy Fairchild")

    def test_accepts_legacy_prefixed_id(self):
        provider = _provider({"les_mysteres": _FakeResponse(text=BOOK_HTML_NO_JSON_LD)})
        book = provider.get_book(f"bn:{TOME_1_SLUG}")
        assert book is not None
        assert book.provider_id == TOME_1_SLUG

    @pytest.mark.parametrize("book_id", ["", "../admin", "serie/harry-potter", "nope:123"])
    def test_rejects_invalid_ids_without_request(self, book_id):
        provider = _provider({})
        assert provider.get_book(book_id) is None
        assert provider.session.calls == []
