"""Booknode metadata provider. French community book catalog, no API key required.

Booknode covers French self-published and small-press authors (e.g. Amazon
KDP-only cozy mystery writers) that Google Books and Open Library mostly miss.

Booknode has no public API, so this reads the site. Pages used:
    /qsearch/ajax_quicksearch.php?search=...   the site's quick search: JSON with
                                               up to 5 books and author suggestions,
                                               no bot challenge. ISBNs are not indexed.
    /<title_slug>_<id>                         book page, schema.org JSON-LD + HTML
    /auteur/<slug>/livres                      an author's bibliography
    /serie/<slug>                              a series and its tomes

The full ``/search`` page renders its results with JavaScript, so it is not used.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from shelfmark.core.cache import cacheable
from shelfmark.core.logger import setup_logger
from shelfmark.core.settings_registry import (
    ActionButton,
    CheckboxField,
    HeadingField,
    SettingsField,
    register_settings,
)
from shelfmark.download.network import get_proxies, get_ssl_verify
from shelfmark.metadata_providers import (
    BookMetadata,
    DisplayField,
    MetadataProvider,
    MetadataSearchOptions,
    SearchField,
    SearchResult,
    SearchType,
    SortOrder,
    TextSearchField,
    register_provider,
)
from shelfmark.metadata_providers.openlibrary import RateLimiter

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = setup_logger(__name__)

BOOKNODE_BASE_URL = "https://booknode.com"
BOOKNODE_QUICKSEARCH_URL = f"{BOOKNODE_BASE_URL}/qsearch/ajax_quicksearch.php"
BOOKNODE_SERIES_URL_PREFIX = f"{BOOKNODE_BASE_URL}/serie/"

# Community-run site that publishes no limit; stay polite.
RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 15

REQUEST_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
}

# Legacy id prefix: the fork's first version served Booknode results from the
# "babelio" provider as "bn:<slug>". Still accepted so saved ids keep resolving.
LEGACY_ID_PREFIX = "bn:"

ISBN_13_LENGTH = 13

# "Series, Tome N : Title". Half-tome positions use a French decimal comma ("Tome 0,5").
_SERIES_TITLE_RE = re.compile(
    r"^(?P<series>.+?),\s*Tome\s*(?P<position>\d+(?:[.,]\d+)?)\s*:\s*(?P<title>.+)$",
    re.IGNORECASE,
)

# A book page is the bare domain + title slug + numeric id, e.g.
# "https://booknode.com/les_heritiers_de_laube_03646884". Series pages ("/serie/<slug>")
# and comment permalinks ("<book url>/commentaires/<id>") do not match.
_BOOK_SLUG_RE = re.compile(r"[a-z0-9_]+_\d+")
_BOOK_URL_RE = re.compile(rf"https://booknode\.com/{_BOOK_SLUG_RE.pattern}")

# Search thumbnails are 30x40 ("/mod11/<name>-<n>-30-40.webp"); the CDN serves the same
# cover at 264x432, the size book pages use.
_COVER_SIZE_RE = re.compile(r"-\d+-\d+\.(?:webp|jpe?g|png)$", re.IGNORECASE)
_COVER_SIZE = "-264-432.jpg"

_rate_limiter = RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)


def clean_text(value: object) -> str:
    """Collapse whitespace; non-strings become ''."""
    return " ".join(value.split()) if isinstance(value, str) else ""


def normalize_for_match(value: str | None) -> str:
    """Accent-insensitive, punctuation-insensitive comparison form."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = "".join(char if char.isalnum() else " " for char in value)
    return " ".join(value.lower().split())


def split_series_title(name: str) -> tuple[str | None, float | None, str]:
    """Split a Booknode "Series, Tome N : Title" string into its parts."""
    match = _SERIES_TITLE_RE.match(name)
    if not match:
        return None, None, name.strip()
    try:
        position: float | None = float(match.group("position").replace(",", "."))
    except ValueError:
        position = None
    return match.group("series").strip(), position, match.group("title").strip()


def _slug_from_url(url: str) -> str | None:
    """Path of a Booknode URL, which doubles as the book (or series) id."""
    path = urlparse(url).path.strip("/")
    return path or None


def _upscale_cover(url: object) -> str | None:
    """Turn a tiny search thumbnail URL into the 264x432 cover URL."""
    if not isinstance(url, str) or not url:
        return None
    if "/book_cover/" not in url:
        return url
    return _COVER_SIZE_RE.sub(_COVER_SIZE, url.replace("/mod11/", "/"))


def valid_isbn(candidate: object) -> str | None:
    """Return a normalized ISBN-10/13, else None."""
    digits = re.sub(r"[\s-]", "", str(candidate or "")).upper()
    if len(digits) == ISBN_13_LENGTH and digits.isdigit():
        return digits
    if re.fullmatch(r"\d{9}[\dX]", digits):
        return digits
    return None


def is_confident_author_match(author_name: str, query: str) -> bool:
    """Whether Booknode's suggested author is really who the query is about.

    The quick search suggests an author for nearly any query sharing one word with
    their name (a title containing "des" suggests "Guy Des Cars"). Requiring the
    normalized name to contain, or be contained in, the query keeps real author
    searches ("Ava Manceau", "Manceau") and rejects those single-word overlaps.
    """
    normalized_author = normalize_for_match(author_name)
    normalized_query = normalize_for_match(query)
    if not normalized_author or not normalized_query:
        return False
    return normalized_author in normalized_query or normalized_query in normalized_author


def _item_matches_author(item: dict[str, Any], author_href: str) -> bool:
    """Whether a quick-search book lists the given author (by profile URL)."""
    return any(
        isinstance(a, dict) and a.get("href") == author_href for a in (item.get("authors") or [])
    )


def json_ld_book(soup: BeautifulSoup) -> dict[str, Any]:
    """Return the page's schema.org Book object, or {}."""
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        for candidate in data if isinstance(data, list) else [data]:
            if isinstance(candidate, dict) and candidate.get("@type") == "Book":
                return candidate
    return {}


def _dedup(books: Iterable[BookMetadata | None]) -> list[BookMetadata]:
    result: list[BookMetadata] = []
    seen: set[str] = set()
    for book in books:
        if book and book.provider_id not in seen:
            seen.add(book.provider_id)
            result.append(book)
    return result


@register_provider("booknode")
class BooknodeProvider(MetadataProvider):
    """Booknode metadata provider (French community catalog, read from the site)."""

    name = "booknode"
    display_name = "Booknode"
    requires_auth = False
    supported_sorts: ClassVar[tuple[SortOrder, ...]] = (SortOrder.RELEVANCE,)
    search_fields: ClassVar[tuple[SearchField, ...]] = (
        TextSearchField(
            key="author",
            label="Author",
            description="Search by author name",
        ),
        TextSearchField(
            key="title",
            label="Title",
            description="Search by book title",
        ),
    )

    def __init__(self) -> None:
        """Initialize provider."""
        self.session = requests.Session()
        self.session.headers.update(REQUEST_HEADERS)

    def is_available(self) -> bool:
        """Booknode needs no authentication."""
        return True

    def _get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response | None:
        _rate_limiter.wait_if_needed()
        try:
            response = self.session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
                proxies=get_proxies(url),
                verify=get_ssl_verify(url),
            )
            response.raise_for_status()
        except requests.Timeout:
            logger.warning("Booknode request timed out: %s", url)
            return None
        except requests.RequestException:
            logger.exception("Booknode request failed: %s", url)
            return None
        return response

    # ------------------------------------------------------------------ search

    def search(self, options: MetadataSearchOptions) -> list[BookMetadata]:
        """Search Booknode by title and/or author."""
        return self.search_paginated(options).books

    def search_paginated(self, options: MetadataSearchOptions) -> SearchResult:
        """Search, slicing one page out of the result list.

        Booknode has no paginated search, so each page re-runs the search with a
        larger cap and keeps its own slice; ``has_more`` is exact.
        """
        page = max(options.page, 1)
        limit = max(options.limit, 1)

        if options.search_type == SearchType.ISBN:
            result = self.search_by_isbn(options.query)
            books = [result] if result and page == 1 else []
            return SearchResult(books=books, page=page, total_found=len(books))

        author = clean_text(options.fields.get("author"))
        title = clean_text(options.fields.get("title"))
        query = clean_text(options.query)
        if options.search_type == SearchType.AUTHOR and not author:
            author, query = query, ""
        if not (author or title or query):
            return SearchResult(books=[], page=page)

        start = (page - 1) * limit
        wanted = start + limit + 1  # one extra tells whether another page exists
        cache_key = f"{query}:{title}:{author}:{wanted}"
        books = self._search_cached(cache_key, query, title, author, wanted) or []
        return SearchResult(
            books=books[start : start + limit],
            page=page,
            total_found=len(books) if len(books) < wanted else 0,
            has_more=len(books) > start + limit,
        )

    @cacheable(ttl_key="METADATA_CACHE_SEARCH_TTL", ttl_default=300, key_prefix="booknode:search")
    def _search_cached(
        self, cache_key: str, query: str, title: str, author: str, wanted: int
    ) -> list[BookMetadata] | None:
        # Returns None (not []) on fetch failure so the failure is not cached.
        author_only = bool(author) and not title and not query
        full_query = " ".join(t for t in (title or query, author) if t)

        data = self._quicksearch(full_query)
        if data is None:
            return None
        raw_books = [b for b in data.get("book") or [] if isinstance(b, dict)]

        suggested = next((a for a in data.get("author") or [] if isinstance(a, dict)), None)
        if suggested and not is_confident_author_match(
            suggested.get("name", ""), author or full_query
        ):
            suggested = None

        if suggested is None:
            books = _dedup(self._parse_search_item(item) for item in raw_books)
            if author:
                wanted_author = normalize_for_match(author)
                books = [
                    b
                    for b in books
                    if any(wanted_author in normalize_for_match(a) for a in b.authors)
                ]
        else:
            author_href = suggested.get("href", "")
            books = _dedup(
                self._parse_search_item(item)
                for item in raw_books
                if _item_matches_author(item, author_href)
            )
            if author_only or not books:
                # An author search: the 5-result quick search rarely lists all of
                # their books, so read their bibliography page instead.
                books = self._fetch_author_books(suggested, wanted) or books

        logger.info("Booknode search '%s' returned %s results", full_query, len(books))
        return books[:wanted]

    def _quicksearch(self, query: str) -> dict[str, Any] | None:
        response = self._get(BOOKNODE_QUICKSEARCH_URL, params={"search": query})
        if response is None:
            return None
        try:
            data = response.json()
        except ValueError:
            logger.warning("Booknode quick search returned a non-JSON response")
            return None
        return data if isinstance(data, dict) else None

    def _build_book(
        self,
        *,
        slug: str,
        name: str,
        authors: list[str],
        cover_url: str | None,
    ) -> BookMetadata:
        series_name, series_position, title = split_series_title(name)
        display_fields = []
        if series_name:
            label = f"{series_name} {series_position:g}" if series_position else series_name
            display_fields.append(DisplayField(label="Series", value=label, icon="editions"))

        return BookMetadata(
            provider=self.name,
            provider_id=slug,
            provider_display_name=self.display_name,
            title=title,
            authors=authors,
            cover_url=cover_url,
            source_url=f"{BOOKNODE_BASE_URL}/{slug}",
            language="fr",
            series_name=series_name,
            series_position=series_position,
            search_title=title,
            search_author=authors[0] if authors else None,
            display_fields=display_fields,
        )

    def _parse_search_item(self, item: dict[str, Any]) -> BookMetadata | None:
        name = clean_text(item.get("name"))
        href = item.get("href")
        if not name or not isinstance(href, str) or not _BOOK_URL_RE.fullmatch(href):
            return None
        slug = _slug_from_url(href)
        if not slug:
            return None
        authors = [
            clean_text(a.get("nom"))
            for a in item.get("authors") or []
            if isinstance(a, dict) and clean_text(a.get("nom"))
        ]
        return self._build_book(
            slug=slug, name=name, authors=authors, cover_url=_upscale_cover(item.get("img"))
        )

    def _fetch_author_books(self, author: dict[str, Any], wanted: int) -> list[BookMetadata]:
        """Read an author's bibliography (``/auteur/<slug>/livres``).

        That page lists standalone books directly, but each series only as one card
        linking to the series; its tomes take one more request per series. Series are
        expanded in page order and only until ``wanted`` books are collected.
        """
        author_href = author.get("href")
        if not isinstance(author_href, str) or not author_href.startswith(BOOKNODE_BASE_URL):
            return []
        response = self._get(author_href.rstrip("/") + "/livres")
        if response is None:
            return []

        author_name = clean_text(author.get("name")) or None
        books, series_urls = self._parse_author_books_page(response.text, author_name)
        seen = {book.provider_id for book in books}

        for series_url in series_urls:
            if len(books) >= wanted:
                break
            series_response = self._get(series_url)
            if series_response is None:
                continue
            for book in self._parse_series_page(series_response.text, author_name):
                if book.provider_id not in seen:
                    seen.add(book.provider_id)
                    books.append(book)

        return books[:wanted]

    def _parse_author_books_page(
        self, page_html: str, author_name: str | None
    ) -> tuple[list[BookMetadata], list[str]]:
        """Split a bibliography page into standalone books and series URLs.

        "Toutes les séries" and "Tous les livres" use the same ``.panel-book-card``
        widget; the cover link tells them apart (book URL vs ``/serie/<slug>``).
        """
        soup = BeautifulSoup(page_html, "html.parser")
        books: list[BookMetadata] = []
        series_urls: list[str] = []
        seen: set[str] = set()

        for card in soup.select(".panel-book-card"):
            cover_link = card.select_one(".cover-wrapper a[href]")
            if cover_link is None:
                continue
            href = str(cover_link.get("href") or "")

            if href.startswith(BOOKNODE_SERIES_URL_PREFIX):
                if href not in series_urls:
                    series_urls.append(href)
                continue
            if not _BOOK_URL_RE.fullmatch(href):
                continue

            slug = _slug_from_url(href)
            title_link = card.select_one(".title a[href]")
            name = clean_text(title_link.get_text()) if title_link else ""
            if not slug or not name or slug in seen:
                continue
            seen.add(slug)

            img = card.select_one(".cover-wrapper img")
            cover = (img.get("data-src") or img.get("src")) if img is not None else None
            books.append(
                self._build_book(
                    slug=slug,
                    name=name,
                    authors=[author_name] if author_name else [],
                    cover_url=_upscale_cover(clean_text(cover)),
                )
            )

        return books, series_urls

    def _parse_series_page(self, page_html: str, author_name: str | None) -> list[BookMetadata]:
        """Read every tome listed under "La liste des tomes" on a series page."""
        soup = BeautifulSoup(page_html, "html.parser")
        books: list[BookMetadata] = []
        seen: set[str] = set()

        for cover_link in soup.select("article.liste .book a.main_cover_link[href]"):
            href = str(cover_link.get("href") or "")
            if not _BOOK_URL_RE.fullmatch(href):
                continue
            slug = _slug_from_url(href)
            name = clean_text(cover_link.get("title"))
            if not slug or not name or slug in seen:
                continue
            seen.add(slug)

            img = cover_link.find("img")
            cover = (img.get("data-src") or img.get("src")) if img is not None else None
            books.append(
                self._build_book(
                    slug=slug,
                    name=name,
                    authors=[author_name] if author_name else [],
                    cover_url=_upscale_cover(clean_text(cover)),
                )
            )

        return books

    def search_by_isbn(self, isbn: str) -> BookMetadata | None:
        """Booknode's search does not index ISBNs, so this never finds a book."""
        logger.debug("Booknode cannot search by ISBN (%s)", isbn)
        return None

    # ------------------------------------------------------------------ book

    @cacheable(ttl_key="METADATA_CACHE_BOOK_TTL", ttl_default=600, key_prefix="booknode:book")
    def get_book(self, book_id: str) -> BookMetadata | None:
        """Get full book details from its Booknode page."""
        slug = book_id.removeprefix(LEGACY_ID_PREFIX)
        if not _BOOK_SLUG_RE.fullmatch(slug):
            logger.warning("Invalid Booknode book id: %s", book_id)
            return None
        response = self._get(f"{BOOKNODE_BASE_URL}/{slug}")
        if response is None:
            return None
        return self._parse_book_page(response.text, slug)

    def _parse_book_page(self, page_html: str, slug: str) -> BookMetadata | None:
        """Parse a book page: JSON-LD first, HTML for what it lacks."""
        soup = BeautifulSoup(page_html, "html.parser")
        ld = json_ld_book(soup)

        h1 = soup.find("h1")
        name = clean_text(ld.get("name")) or (clean_text(h1.get_text()) if h1 else "")
        if not name:
            logger.warning("Booknode book page has no title: %s", slug)
            return None
        series_name, series_position, title = split_series_title(name)

        series_id = None
        part_of = ld.get("isPartOf")
        if isinstance(part_of, dict):
            series_name = series_name or clean_text(part_of.get("name")) or None
            if series_position is None and isinstance(part_of.get("position"), int | float):
                series_position = float(part_of["position"])
            if isinstance(part_of.get("url"), str):
                series_id = _slug_from_url(part_of["url"])
        if series_id is None:
            series_link = soup.select_one('a[href*="/serie/"]')
            if series_link is not None:
                series_id = _slug_from_url(str(series_link.get("href") or ""))

        ld_authors = ld.get("author")
        authors = [
            clean_text(a.get("name"))
            for a in (ld_authors if isinstance(ld_authors, list) else [ld_authors])
            if isinstance(a, dict) and clean_text(a.get("name"))
        ] or [clean_text(a.get_text()) for a in soup.select('a[href*="/auteur/"]')]
        authors = list(dict.fromkeys(a for a in authors if a))

        ld_genres = ld.get("genre")
        if isinstance(ld_genres, list):
            genres = [clean_text(g) for g in ld_genres]
        else:
            genres = [clean_text(a.get_text()) for a in soup.select('a[href*="/theme/"]')]
        genres = list(dict.fromkeys(g for g in genres if g))

        # JSON-LD's image is the multi-megabyte original; the page shows a 264x432 copy.
        cover_img = soup.select_one(".main-cover img")
        cover_url = clean_text(cover_img.get("src")) if cover_img is not None else ""

        publisher_link = soup.select_one('a[href^="/editeur/"]')
        publisher = clean_text(publisher_link.get_text()) if publisher_link is not None else ""
        publish_year = None
        year_match = re.match(r"(\d{4})", str(ld.get("datePublished") or ""))
        if year_match:
            publish_year = int(year_match.group(1))

        isbn = valid_isbn(ld.get("isbn"))
        description = (
            html.unescape(ld["description"]).strip()
            if isinstance(ld.get("description"), str) and ld["description"].strip()
            else self._parse_description(soup)
        )

        return BookMetadata(
            provider=self.name,
            provider_id=slug,
            provider_display_name=self.display_name,
            title=title,
            authors=authors,
            isbn_13=isbn if isbn and len(isbn) == ISBN_13_LENGTH else None,
            isbn_10=isbn if isbn and len(isbn) != ISBN_13_LENGTH else None,
            cover_url=cover_url or None,
            description=description,
            publisher=publisher or None,
            publish_year=publish_year,
            language=clean_text(ld.get("inLanguage")) or "fr",
            genres=genres,
            source_url=f"{BOOKNODE_BASE_URL}/{slug}",
            series_id=series_id,
            series_name=series_name,
            series_position=series_position,
            search_title=title,
            search_author=authors[0] if authors else None,
            display_fields=self._rating_fields(ld.get("aggregateRating")),
        )

    @staticmethod
    def _rating_fields(rating: object) -> list[DisplayField]:
        if not isinstance(rating, dict):
            return []
        fields = []
        value, best = rating.get("ratingValue"), rating.get("bestRating") or 10
        if isinstance(value, int | float) and isinstance(best, int | float):
            fields.append(DisplayField(label="Rating", value=f"{value:g}/{best:g}", icon="star"))
        count = rating.get("ratingCount")
        if isinstance(count, int) and count > 0:
            fields.append(DisplayField(label="Ratings", value=f"{count:,}", icon="users"))
        return fields

    @staticmethod
    def _parse_description(soup: BeautifulSoup) -> str | None:
        """Fallback blurb: the pinned comment that starts with a "Résumé" label."""
        marker = soup.select_one(".resume-title")
        if marker is None:
            return None
        container = marker.find_parent(class_="actual-text")
        if container is None:
            return None
        marker.extract()
        paragraphs = [p for p in (clean_text(t) for t in container.stripped_strings) if p]
        return "\n".join(paragraphs) or None


def _test_booknode_connection() -> dict[str, Any]:
    """Test the Booknode quick search endpoint."""
    try:
        response = requests.get(
            BOOKNODE_QUICKSEARCH_URL,
            params={"search": "test"},
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
            proxies=get_proxies(BOOKNODE_BASE_URL),
            verify=get_ssl_verify(BOOKNODE_BASE_URL),
        )
        response.raise_for_status()
        data = response.json()
    except requests.Timeout:
        return {"success": False, "message": "Connection timed out"}
    except requests.RequestException as e:
        return {"success": False, "message": f"Connection failed: {e}"}
    except ValueError as e:
        return {"success": False, "message": f"Unexpected response: {e}"}
    if isinstance(data, dict) and "book" in data:
        return {"success": True, "message": "Successfully connected to Booknode"}
    return {"success": False, "message": "Unexpected response from Booknode"}


@register_settings("booknode", "Booknode", icon="library", order=56, group="metadata_providers")
def booknode_settings() -> list[SettingsField]:
    """Booknode metadata provider settings."""
    return [
        HeadingField(
            key="booknode_heading",
            title="Booknode",
            description=(
                "French community book catalog. Good coverage of French self-published "
                "and small-press authors that Google Books and Open Library tend to miss. "
                "No API key required. Title searches return at most 5 books (a limit of "
                "Booknode's search); author searches list the author's whole bibliography. "
                "Booknode does not index ISBNs."
            ),
            link_url=BOOKNODE_BASE_URL,
            link_text="booknode.com",
        ),
        CheckboxField(
            key="BOOKNODE_ENABLED",
            label="Enable Booknode",
            description="Enable Booknode as a metadata provider for book searches",
            default=False,
        ),
        ActionButton(
            key="test_connection",
            label="Test Connection",
            description="Verify Booknode's search is accessible",
            style="primary",
            callback=_test_booknode_connection,
        ),
    ]
