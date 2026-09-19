"""Francophone metadata provider for self-published / small-press books.

Google Books and Open Library both have poor coverage of French self-published
authors (e.g. Amazon KDP-only cozy mystery writers such as Ava Manceau) - these
titles usually have no ISBN and never enter a traditional publisher catalog.

This provider searches **Booknode** (a French community book catalog) as its
primary source: it exposes a plain, unauthenticated JSON endpoint
(``/qsearch/ajax_quicksearch.php``) that serves live search results without
any bot-detection challenge.

**Babelio** is used as a secondary, best-effort source only. In practice
Babelio serves an interactive anti-bot "security verification" (slider
captcha) on every search and book page to non-browser clients, so calls to it
will very likely return no results in most deployments. This module does not
attempt to solve or bypass that challenge - it detects the challenge page and
degrades to "no results" instead of crashing, and only exists so that
Babelio can start contributing results automatically if that ever changes
(e.g. a future official API, or a network path that isn't challenged).
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import deque
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from collections.abc import Iterable

from shelfmark.core.cache import cacheable
from shelfmark.core.logger import setup_logger
from shelfmark.core.settings_registry import (
    ActionButton,
    CheckboxField,
    HeadingField,
    SettingsField,
    register_settings,
)
from shelfmark.download.network import get_ssl_verify
from shelfmark.metadata_providers import (
    BookMetadata,
    DisplayField,
    MetadataProvider,
    MetadataSearchOptions,
    SearchField,
    SearchType,
    SortOrder,
    TextSearchField,
    register_provider,
)

logger = setup_logger(__name__)

BOOKNODE_BASE_URL = "https://booknode.com"
BOOKNODE_QUICKSEARCH_URL = f"{BOOKNODE_BASE_URL}/qsearch/ajax_quicksearch.php"

BABELIO_BASE_URL = "https://www.babelio.com"
BABELIO_SEARCH_URL = f"{BABELIO_BASE_URL}/resrecherche.php"

# Both sites are small/community-run - stay polite regardless of which one answers.
RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW_SECONDS = 60

REQUEST_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
}

REQUEST_TIMEOUT_SECONDS = 10

# Matches Booknode's combined "Series, Tome N : Title" string into its three parts.
_SERIES_TITLE_RE = re.compile(
    r"^(?P<series>.+?),\s*Tome\s*(?P<position>[\d.]+)\s*:\s*(?P<title>.+)$",
    re.IGNORECASE,
)

# Babelio book URLs look like /livres/<Author-Slug-Title>/<numeric id> - this is the one
# piece of Babelio's markup confirmed from a live (non-challenged) URL; everything else
# about their result-page markup is unverified because search/listing pages are
# captcha-gated (see module docstring).
_BABELIO_BOOK_HREF_RE = re.compile(r'href="(/livres/[^/"]+/(\d+))"[^>]*>([^<]+)<')

# Substring of Babelio's anti-bot interstitial title, byte-safe regardless of the
# page's declared (often mismatched) charset.
_BABELIO_CHALLENGE_MARKER = "rification de s"

# A Booknode book page URL is always the bare domain + slug + numeric id, e.g.
# "https://booknode.com/les_heritiers_de_laube_03646884". Series pages
# ("/serie/<slug>") and comment permalinks ("<book url>/commentaires/<id>") don't
# match this, which is exactly how author-page scraping tells them apart below.
_BOOKNODE_BOOK_URL_RE = re.compile(r"^https://booknode\.com/[a-z0-9_]+_\d+$")


class RateLimiter:
    """Simple sliding window rate limiter."""

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        """Initialize rate limiter with max requests per time window."""
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.timestamps: deque[float] = deque()
        self.lock = threading.Lock()

    def wait_if_needed(self) -> None:
        """Block until a request is allowed (thread-safe)."""
        wait_time = 0.0

        with self.lock:
            now = time.time()
            cutoff = now - self.window_seconds
            while self.timestamps and self.timestamps[0] < cutoff:
                self.timestamps.popleft()
            if len(self.timestamps) >= self.max_requests:
                wait_time = self.timestamps[0] + self.window_seconds - now

        if wait_time > 0:
            logger.debug("Rate limited, waiting %0.2fs", wait_time)
            time.sleep(wait_time)

        with self.lock:
            now = time.time()
            cutoff = now - self.window_seconds
            while self.timestamps and self.timestamps[0] < cutoff:
                self.timestamps.popleft()
            self.timestamps.append(time.time())


_rate_limiter = RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)


def _clean_text(value: str | None) -> str | None:
    """Collapse whitespace in scraped text."""
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _split_series_title(name: str) -> tuple[str | None, float | None, str]:
    """Split a Booknode "Series, Tome N : Title" string into its parts."""
    match = _SERIES_TITLE_RE.match(name)
    if not match:
        return None, None, name.strip()

    series = match.group("series").strip()
    title = match.group("title").strip()
    try:
        position = float(match.group("position"))
    except ValueError:
        position = None
    return series, position, title


def _booknode_slug_from_href(href: str) -> str | None:
    """Extract the path slug (which doubles as Booknode's book id) from a full URL."""
    path = urlparse(href).path.strip("/")
    return path or None


def _normalize_for_match(value: str) -> str:
    """Accent-insensitive, punctuation-insensitive comparison form."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = "".join(char if char.isalnum() else " " for char in value)
    return " ".join(value.lower().split())


def _is_confident_author_match(author_name: str, query: str) -> bool:
    """Whether Booknode's suggested author is actually who the query is about.

    Booknode's quicksearch suggests an "author" for almost any query that shares even
    one word with an author's name (e.g. searching a title containing "des" once
    suggested the unrelated author "Guy Des Cars") - that noise must not be allowed to
    hijack an ordinary title search. Requiring the (normalized) author name to fully
    contain, or be contained in, the query keeps genuine author searches ("Ava
    Manceau", "Manceau", "livres de Ava Manceau") while rejecting coincidental
    single-word overlaps.
    """
    normalized_author = _normalize_for_match(author_name)
    normalized_query = _normalize_for_match(query)
    if not normalized_author or not normalized_query:
        return False
    return normalized_author in normalized_query or normalized_query in normalized_author


def _item_matches_author(item: dict, author_href: str) -> bool:
    """Whether a Booknode search-result book lists the given author (by profile URL)."""
    return any(
        isinstance(a, dict) and a.get("href") == author_href for a in (item.get("authors") or [])
    )


@register_provider("babelio")
class BabelioSearchProvider(MetadataProvider):
    """Francophone metadata provider: Booknode (primary) with Babelio as a fallback.

    Internal provider ids are prefixed to tell the two backends apart:
    ``bn:<slug>`` for Booknode, ``bab:<id>`` for Babelio.
    """

    name = "babelio"
    display_name = "Babelio"
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
        """No authentication or extra dependencies are required."""
        return True

    def _get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response | None:
        _rate_limiter.wait_if_needed()
        try:
            response = self.session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
                verify=get_ssl_verify(url),
            )
            response.raise_for_status()
        except requests.Timeout:
            logger.warning("Request timed out: %s", url)
            return None
        except requests.RequestException:
            logger.exception("Request failed: %s", url)
            return None
        return response

    # -- Search -----------------------------------------------------------------

    def search(self, options: MetadataSearchOptions) -> list[BookMetadata]:
        """Search Booknode first, falling back to Babelio if it finds nothing."""
        if options.search_type == SearchType.ISBN:
            result = self.search_by_isbn(options.query)
            return [result] if result else []

        author_value = (options.fields.get("author") or "").strip()
        title_value = (options.fields.get("title") or "").strip()
        query = " ".join(t for t in (title_value, author_value) if t) or options.query.strip()
        if not query:
            return []

        fields_key = ":".join(f"{k}={v}" for k, v in sorted(options.fields.items()))
        cache_key = f"{query}:{options.limit}:{fields_key}"
        return self._search_cached(cache_key, query, options.limit) or []

    @cacheable(ttl_key="METADATA_CACHE_SEARCH_TTL", ttl_default=300, key_prefix="babelio:search")
    def _search_cached(self, cache_key: str, query: str, limit: int) -> list[BookMetadata]:
        books = self._search_booknode(query, limit)
        if books:
            return books

        logger.debug("Booknode returned no results for '%s', trying Babelio", query)
        return self._search_babelio(query, limit)

    def _search_booknode(self, query: str, limit: int) -> list[BookMetadata]:
        response = self._get(BOOKNODE_QUICKSEARCH_URL, params={"search": query})
        if response is None:
            return []

        try:
            data = response.json()
        except ValueError:
            logger.warning("Booknode quicksearch returned non-JSON response")
            return []

        raw_books = data.get("book", [])
        raw_authors = data.get("author", [])

        best_author = raw_authors[0] if raw_authors else None
        if best_author and not _is_confident_author_match(best_author.get("name", ""), query):
            # e.g. searching a title containing "des" once suggested the unrelated
            # author "Guy Des Cars" - not a real author search, ignore the suggestion.
            best_author = None

        if best_author:
            author_href = best_author.get("href", "")
            books = self._dedup_books(
                self._parse_booknode_search_item(item)
                for item in raw_books
                if _item_matches_author(item, author_href)
            )
            if not books:
                # A pure author-name search: the fuzzy title index has nothing of
                # hers, so fetch her actual Booknode bibliography instead.
                books = self._fetch_booknode_author_books(best_author, limit)
            logger.info(
                "Booknode search '%s' matched author '%s': %s results",
                query,
                best_author.get("name"),
                len(books),
            )
            return books[:limit]

        books = self._dedup_books(self._parse_booknode_search_item(item) for item in raw_books)
        logger.info("Booknode search '%s' returned %s results", query, len(books))
        return books[:limit]

    def _dedup_books(self, books: Iterable[BookMetadata | None]) -> list[BookMetadata]:
        result: list[BookMetadata] = []
        seen: set[str] = set()
        for book in books:
            if book and book.provider_id not in seen:
                seen.add(book.provider_id)
                result.append(book)
        return result

    def _build_booknode_book(
        self,
        *,
        slug: str,
        name: str,
        authors: list[str],
        cover_url: str | None,
        source_url: str,
    ) -> BookMetadata:
        series_name, series_position, title = _split_series_title(name)

        display_fields = []
        if series_name:
            position_label = (
                f"{series_name} {series_position:g}" if series_position else series_name
            )
            display_fields.append(
                DisplayField(label="Series", value=position_label, icon="editions")
            )

        return BookMetadata(
            provider=self.name,
            provider_id=f"bn:{slug}",
            provider_display_name=self.display_name,
            title=title,
            authors=authors,
            cover_url=cover_url,
            source_url=source_url,
            language="fr",
            series_name=series_name,
            series_position=series_position,
            search_title=title,
            search_author=authors[0] if authors else None,
            display_fields=display_fields,
        )

    def _parse_booknode_search_item(self, item: dict) -> BookMetadata | None:
        try:
            name = item.get("name")
            href = item.get("href")
            if not name or not href:
                return None

            slug = _booknode_slug_from_href(href)
            if not slug:
                return None

            authors = [
                a["nom"] for a in item.get("authors", []) if isinstance(a, dict) and a.get("nom")
            ]

            return self._build_booknode_book(
                slug=slug,
                name=name,
                authors=authors,
                cover_url=item.get("img"),
                source_url=href,
            )
        except (TypeError, ValueError, AttributeError, KeyError) as e:
            logger.debug("Failed to parse Booknode search item: %s", e)
            return None

    def _fetch_booknode_author_books(self, author: dict, limit: int) -> list[BookMetadata]:
        """Fetch an author's Booknode profile page and extract her clean book links."""
        href = author.get("href")
        if not href:
            return []

        response = self._get(href)
        if response is None:
            return []

        return self._parse_booknode_author_page(response.text, author.get("name"))[:limit]

    def _parse_booknode_author_page(
        self, html: str, author_name: str | None
    ) -> list[BookMetadata]:
        """Extract an author's cleanly-linked books from her Booknode profile page.

        Only Booknode's "latest release" / "next release" panels and her standalone
        (non-series) books carousel put a full ``title`` attribute on the book anchor;
        the page's series carousel links to each series as a whole (not a specific
        book) and its activity feed links to comment permalinks, so both are skipped by
        requiring a clean book URL (see ``_BOOKNODE_BOOK_URL_RE``) with a ``title``
        attribute. A practical consequence: an older mid-series tome that is neither
        the latest nor the next release may not surface here - a direct title search
        still finds those.
        """
        soup = BeautifulSoup(html, "html.parser")
        books: list[BookMetadata] = []
        seen: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"])
            if not _BOOKNODE_BOOK_URL_RE.fullmatch(href):
                continue

            title_attr = _clean_text(anchor.get("title"))
            if not title_attr:
                continue

            slug = _booknode_slug_from_href(href)
            if not slug or slug in seen:
                continue
            seen.add(slug)

            cover_url = None
            img = anchor.find("img")
            if img is not None:
                cover_url = img.get("data-src") or img.get("src")
                if cover_url and str(cover_url).startswith("data:"):
                    cover_url = img.get("data-src")

            books.append(
                self._build_booknode_book(
                    slug=slug,
                    name=title_attr,
                    authors=[author_name] if author_name else [],
                    cover_url=cover_url,
                    source_url=href,
                )
            )

        return books

    def _search_babelio(self, query: str, limit: int) -> list[BookMetadata]:
        """Best-effort Babelio search - see module docstring for the anti-bot caveat."""
        response = self._get(BABELIO_SEARCH_URL, params={"Recherche": query})
        if response is None:
            return []

        if _BABELIO_CHALLENGE_MARKER in response.text:
            logger.warning(
                "Babelio served its anti-bot verification page instead of results; "
                "skipping (Booknode already returned no results for '%s')",
                query,
            )
            return []

        books: list[BookMetadata] = []
        seen: set[str] = set()
        for match in _BABELIO_BOOK_HREF_RE.finditer(response.text):
            href, book_id, anchor_text = match.group(1), match.group(2), match.group(3)
            if book_id in seen:
                continue
            title = _clean_text(anchor_text)
            if not title:
                continue
            seen.add(book_id)
            books.append(
                BookMetadata(
                    provider=self.name,
                    provider_id=f"bab:{book_id}",
                    provider_display_name=self.display_name,
                    title=title,
                    source_url=BABELIO_BASE_URL + href,
                    language="fr",
                    search_title=title,
                )
            )
            if len(books) >= limit:
                break

        logger.info("Babelio search '%s' returned %s results", query, len(books))
        return books

    @cacheable(ttl_key="METADATA_CACHE_BOOK_TTL", ttl_default=600, key_prefix="babelio:isbn")
    def search_by_isbn(self, isbn: str) -> BookMetadata | None:
        """Search by ISBN/EAN using the same site search (best-effort, ISBN often absent)."""
        clean_isbn = isbn.replace("-", "").strip()
        if not clean_isbn:
            return None

        books = self._search_booknode(clean_isbn, limit=1)
        if books:
            return self.get_book(books[0].provider_id)

        books = self._search_babelio(clean_isbn, limit=1)
        return books[0] if books else None

    # -- Book detail --------------------------------------------------------------

    @cacheable(ttl_key="METADATA_CACHE_BOOK_TTL", ttl_default=600, key_prefix="babelio:book")
    def get_book(self, book_id: str) -> BookMetadata | None:
        """Get full book details. Dispatches to Booknode or Babelio by id prefix."""
        if book_id.startswith("bn:"):
            return self._get_booknode_book(book_id[len("bn:") :])
        if book_id.startswith("bab:"):
            return self._get_babelio_book(book_id[len("bab:") :])
        logger.warning("Unrecognized Babelio provider id: %s", book_id)
        return None

    def _get_booknode_book(self, slug: str) -> BookMetadata | None:
        response = self._get(f"{BOOKNODE_BASE_URL}/{slug}")
        if response is None:
            return None

        soup = BeautifulSoup(response.text, "html.parser")

        h1 = soup.find("h1")
        if not h1:
            logger.warning("Booknode book page missing title: %s", slug)
            return None
        series_name, series_position, title = _split_series_title(_clean_text(h1.get_text()) or "")

        authors = [
            _clean_text(a.get_text()) or ""
            for a in soup.select('a[href*="/auteur/"]')
            if a.get_text(strip=True)
        ]
        authors = list(dict.fromkeys(a for a in authors if a))

        series_id = None
        series_link = soup.select_one('a[href*="/serie/"]')
        if series_link:
            series_id = _booknode_slug_from_href(str(series_link.get("href") or ""))

        genres = [
            _clean_text(a.get_text()) or ""
            for a in soup.select('a[href*="/theme/"]')
        ]
        genres = [g for g in genres if g]

        cover_img = soup.select_one(".main-cover img")
        cover_url = str(cover_img.get("src")) if cover_img else None

        description = self._parse_booknode_description(soup)

        return BookMetadata(
            provider=self.name,
            provider_id=f"bn:{slug}",
            provider_display_name=self.display_name,
            title=title,
            authors=authors,
            cover_url=cover_url,
            description=description,
            language="fr",
            genres=genres,
            source_url=f"{BOOKNODE_BASE_URL}/{slug}",
            series_id=series_id,
            series_name=series_name,
            series_position=series_position,
            search_title=title,
            search_author=authors[0] if authors else None,
        )

    def _parse_booknode_description(self, soup: BeautifulSoup) -> str | None:
        """Booknode has no dedicated synopsis field; the official blurb is the first
        pinned comment, marked with a "Résumé" label span.
        """
        marker = soup.select_one(".resume-title")
        if marker is None:
            return None

        container = marker.find_parent(class_="actual-text")
        if container is None:
            return None

        # Drop the "Résumé" label itself, keep the rest of the text.
        marker.extract()
        paragraphs = [p for p in (_clean_text(t) for t in container.stripped_strings) if p]
        return "\n".join(paragraphs) or None

    def _get_babelio_book(self, book_id: str) -> BookMetadata | None:
        """Best-effort Babelio book fetch - see module docstring for the anti-bot caveat."""
        # We don't have the author/title slug portion of the URL here (only the numeric
        # id survives round-tripping through provider_id), so we can't reconstruct the
        # canonical /livres/<slug>/<id> URL without a prior search result. Attempting a
        # plain id lookup for a site we can't get past its captcha for anyway isn't worth
        # a fabricated URL guess.
        logger.debug(
            "Babelio book detail lookup for id %s skipped: no reliable direct URL "
            "(Babelio results only carry the title from search)",
            book_id,
        )
        return None


def _test_booknode_connection() -> dict[str, Any]:
    """Test the Booknode search endpoint (the functional half of this provider)."""
    try:
        provider = BabelioSearchProvider()
        response = provider.session.get(
            BOOKNODE_QUICKSEARCH_URL,
            params={"search": "test"},
            timeout=REQUEST_TIMEOUT_SECONDS,
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
    if "book" in data:
        return {"success": True, "message": "Successfully connected to Booknode"}
    return {"success": False, "message": "Unexpected response from Booknode"}


@register_settings("babelio", "Babelio", icon="library", order=55, group="metadata_providers")
def babelio_settings() -> list[SettingsField]:
    """Babelio metadata provider settings."""
    return [
        HeadingField(
            key="babelio_heading",
            title="Babelio",
            description=(
                "Francophone metadata for books that Google Books and Open Library "
                "tend to miss - self-published and small-press French authors "
                "(e.g. cozy mystery). Backed by Booknode's public search, with "
                "Babelio queried as a fallback when Booknode has no match. Note: "
                "Babelio actively serves an anti-bot verification page on its search "
                "and book pages, so it will often contribute no results - this is a "
                "known limitation of the site, not a bug in this integration."
            ),
            link_url="https://www.babelio.com",
            link_text="babelio.com",
        ),
        CheckboxField(
            key="BABELIO_ENABLED",
            label="Enable Babelio",
            description="Enable Babelio (Booknode + Babelio) as a metadata provider for book searches",
            default=False,
        ),
        ActionButton(
            key="test_connection",
            label="Test Connection",
            description="Verify Booknode's search API is accessible",
            style="primary",
            callback=_test_booknode_connection,
        ),
    ]
