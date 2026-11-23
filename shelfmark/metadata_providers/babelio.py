"""Babelio metadata provider. French reader community catalog, no API key required.

Best effort: Babelio answers non-browser clients with HTTP 403 and an anti-bot
"Vérification de sécurité" slider captcha on its search and book pages. This
provider does not try to solve or get around that challenge. It detects it, backs
off for a while, and (when "Fall back to Booknode" is on, the default) answers the
search with Booknode results instead, so choosing Babelio still finds French books.

Because the result pages are captcha-gated, the markup read here is limited to what
is stable across sites: book links ``/livres/<slug>/<id>`` in search results, and
OpenGraph / schema.org JSON-LD on book pages.

Ids are ``<id>:<slug>`` (the slug is needed to rebuild the book URL). Ids of the
fork's first version (``bn:<slug>`` for Booknode books, ``bab:<id>``) still resolve.
"""

from __future__ import annotations

import html
import re
import threading
import time
from typing import Any, ClassVar

import requests
from bs4 import BeautifulSoup

from shelfmark.core.cache import cacheable
from shelfmark.core.config import config as app_config
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
    MetadataProvider,
    MetadataSearchOptions,
    SearchField,
    SearchResult,
    SearchType,
    SortOrder,
    TextSearchField,
    register_provider,
)
from shelfmark.metadata_providers.booknode import (
    LEGACY_ID_PREFIX as BOOKNODE_LEGACY_ID_PREFIX,
)
from shelfmark.metadata_providers.booknode import (
    REQUEST_HEADERS,
    BooknodeProvider,
    clean_text,
    json_ld_book,
    valid_isbn,
)
from shelfmark.metadata_providers.openlibrary import RateLimiter

logger = setup_logger(__name__)

BABELIO_BASE_URL = "https://www.babelio.com"
BABELIO_SEARCH_URL = f"{BABELIO_BASE_URL}/resrecherche.php"

RATE_LIMIT_REQUESTS = 20
RATE_LIMIT_WINDOW_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 15

# After Babelio serves its captcha, skip it for this long instead of asking again
# on every search.
BLOCKED_BACKOFF_SECONDS = 600

# Substring of the captcha page title ("Vérification de sécurité"), safe whatever
# charset the page declares.
_CHALLENGE_MARKER = "rification de s"
_BLOCKED_STATUS_CODES = frozenset({403, 429})

_BOOK_HREF_RE = re.compile(r"^(?:https://www\.babelio\.com)?/livres/(?P<slug>[^/?#]+)/(?P<id>\d+)")
_BOOK_ID_RE = re.compile(r"^(?P<id>\d+):(?P<slug>[A-Za-z0-9_.~%-]+)$")
_LEGACY_BABELIO_PREFIX = "bab:"

_rate_limiter = RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)
_blocked_lock = threading.Lock()
_blocked_until = 0.0


def _mark_blocked() -> None:
    # Process-wide backoff, shared by every provider instance.
    global _blocked_until
    with _blocked_lock:
        _blocked_until = time.monotonic() + BLOCKED_BACKOFF_SECONDS


def _is_blocked() -> bool:
    with _blocked_lock:
        return time.monotonic() < _blocked_until


def _booknode_fallback_enabled() -> bool:
    return app_config.get("BABELIO_BOOKNODE_FALLBACK", True) is True


@register_provider("babelio")
class BabelioProvider(MetadataProvider):
    """Babelio metadata provider (best effort, falls back to Booknode)."""

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
        self._booknode: BooknodeProvider | None = None

    @property
    def booknode(self) -> BooknodeProvider:
        """Booknode provider used for the fallback and legacy ``bn:`` ids."""
        if self._booknode is None:
            self._booknode = BooknodeProvider()
        return self._booknode

    def is_available(self) -> bool:
        """Babelio needs no authentication."""
        return True

    def _fetch(self, url: str, params: dict[str, Any] | None = None) -> str | None:
        """GET a Babelio page; None on failure or when the captcha is served."""
        if _is_blocked():
            logger.debug("Babelio is in captcha backoff, skipping %s", url)
            return None
        _rate_limiter.wait_if_needed()
        try:
            response = self.session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT_SECONDS,
                proxies=get_proxies(url),
                verify=get_ssl_verify(url),
            )
        except requests.Timeout:
            logger.warning("Babelio request timed out: %s", url)
            return None
        except requests.RequestException:
            logger.exception("Babelio request failed: %s", url)
            return None

        if response.status_code in _BLOCKED_STATUS_CODES or _CHALLENGE_MARKER in response.text:
            _mark_blocked()
            logger.warning(
                "Babelio served its anti-bot verification page (HTTP %s); "
                "skipping Babelio for %s seconds",
                response.status_code,
                BLOCKED_BACKOFF_SECONDS,
            )
            return None
        if not response.ok:
            logger.warning("Babelio returned HTTP %s for %s", response.status_code, url)
            return None
        return response.text

    # ------------------------------------------------------------------ search

    def search(self, options: MetadataSearchOptions) -> list[BookMetadata]:
        """Search Babelio, or Booknode when Babelio has nothing."""
        return self.search_paginated(options).books

    def search_paginated(self, options: MetadataSearchOptions) -> SearchResult:
        """Search Babelio; fall back to Booknode when it is blocked or finds nothing."""
        page = max(options.page, 1)
        limit = max(options.limit, 1)

        if options.search_type == SearchType.ISBN:
            result = self.search_by_isbn(options.query)
            books = [result] if result and page == 1 else []
            return SearchResult(books=books, page=page, total_found=len(books))

        author = clean_text(options.fields.get("author"))
        title = clean_text(options.fields.get("title"))
        query = " ".join(t for t in (title or clean_text(options.query), author) if t)
        if not query:
            return SearchResult(books=[], page=page)

        books = self._search_cached(query, query) or []
        if books:
            start = (page - 1) * limit
            return SearchResult(
                books=books[start : start + limit],
                page=page,
                total_found=len(books),
                has_more=len(books) > start + limit,
            )

        if _booknode_fallback_enabled():
            logger.info("Babelio has no results for '%s', using Booknode", query)
            return self.booknode.search_paginated(options)
        return SearchResult(books=[], page=page)

    @cacheable(ttl_key="METADATA_CACHE_SEARCH_TTL", ttl_default=300, key_prefix="babelio:search")
    def _search_cached(self, cache_key: str, query: str) -> list[BookMetadata] | None:
        # Returns None (not []) on fetch failure so the failure is not cached.
        page_html = self._fetch(BABELIO_SEARCH_URL, params={"Recherche": query})
        if page_html is None:
            return None
        books = self._parse_search_page(page_html)
        logger.info("Babelio search '%s' returned %s results", query, len(books))
        return books

    def _parse_search_page(self, page_html: str) -> list[BookMetadata]:
        soup = BeautifulSoup(page_html, "html.parser")
        books: list[BookMetadata] = []
        seen: set[str] = set()
        for link in soup.select('a[href*="/livres/"]'):
            match = _BOOK_HREF_RE.match(str(link.get("href") or ""))
            title = clean_text(link.get_text())
            if not match or not title or match.group("id") in seen:
                continue
            seen.add(match.group("id"))
            slug, book_id = match.group("slug"), match.group("id")
            books.append(
                BookMetadata(
                    provider=self.name,
                    provider_id=f"{book_id}:{slug}",
                    provider_display_name=self.display_name,
                    title=title,
                    source_url=f"{BABELIO_BASE_URL}/livres/{slug}/{book_id}",
                    language="fr",
                    search_title=title,
                )
            )
        return books

    def search_by_isbn(self, isbn: str) -> BookMetadata | None:
        """Look an ISBN up with Babelio's site search (Booknode cannot search ISBNs)."""
        clean_isbn = re.sub(r"[\s-]", "", isbn or "")
        if not clean_isbn:
            return None
        books = self._search_cached(clean_isbn, clean_isbn)
        return self.get_book(books[0].provider_id) if books else None

    # ------------------------------------------------------------------ book

    def get_book(self, book_id: str) -> BookMetadata | None:
        """Get full book details; legacy ``bn:`` ids are served by Booknode."""
        if book_id.startswith(BOOKNODE_LEGACY_ID_PREFIX):
            return self.booknode.get_book(book_id)
        if book_id.startswith(_LEGACY_BABELIO_PREFIX):
            # The first version kept only the number, which cannot rebuild the URL.
            logger.debug("Babelio id %s has no slug, cannot fetch it", book_id)
            return None
        match = _BOOK_ID_RE.match(book_id)
        if not match:
            logger.warning("Invalid Babelio book id: %s", book_id)
            return None
        return self._get_book_cached(match.group("id"), match.group("slug"))

    @cacheable(ttl_key="METADATA_CACHE_BOOK_TTL", ttl_default=600, key_prefix="babelio:book")
    def _get_book_cached(self, book_id: str, slug: str) -> BookMetadata | None:
        url = f"{BABELIO_BASE_URL}/livres/{slug}/{book_id}"
        page_html = self._fetch(url)
        if page_html is None:
            return None
        return self._parse_book_page(page_html, book_id, slug)

    def _parse_book_page(self, page_html: str, book_id: str, slug: str) -> BookMetadata | None:
        """Read a book page from its JSON-LD and OpenGraph tags."""
        soup = BeautifulSoup(page_html, "html.parser")
        ld = json_ld_book(soup)

        def meta(prop: str) -> str:
            tag = soup.find("meta", attrs={"property": prop}) or soup.find(
                "meta", attrs={"name": prop}
            )
            return clean_text(tag.get("content")) if tag is not None else ""

        h1 = soup.find("h1")
        title = (
            clean_text(ld.get("name"))
            or (clean_text(h1.get_text()) if h1 is not None else "")
            or meta("og:title")
        )
        if not title:
            logger.warning("Babelio book page has no title: %s", book_id)
            return None

        ld_authors = ld.get("author")
        authors = [
            clean_text(a.get("name") if isinstance(a, dict) else a)
            for a in (ld_authors if isinstance(ld_authors, list) else [ld_authors])
        ]
        authors = list(dict.fromkeys(a for a in authors if a))

        description = ld.get("description") if isinstance(ld.get("description"), str) else ""
        description = html.unescape(description or meta("og:description")).strip()

        isbn = valid_isbn(ld.get("isbn"))
        image = ld.get("image")
        cover_url = clean_text(image if isinstance(image, str) else "") or meta("og:image")

        return BookMetadata(
            provider=self.name,
            provider_id=f"{book_id}:{slug}",
            provider_display_name=self.display_name,
            title=title,
            authors=authors,
            isbn_13=isbn if isbn and len(isbn) == 13 else None,
            isbn_10=isbn if isbn and len(isbn) == 10 else None,
            cover_url=cover_url or None,
            description=description or None,
            language="fr",
            source_url=f"{BABELIO_BASE_URL}/livres/{slug}/{book_id}",
            search_title=title,
            search_author=authors[0] if authors else None,
        )


def _test_babelio_connection() -> dict[str, Any]:
    """Test whether Babelio answers searches, and whether the fallback works."""
    try:
        response = requests.get(
            BABELIO_SEARCH_URL,
            params={"Recherche": "test"},
            headers=REQUEST_HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
            proxies=get_proxies(BABELIO_BASE_URL),
            verify=get_ssl_verify(BABELIO_BASE_URL),
        )
    except requests.Timeout:
        return {"success": False, "message": "Connection timed out"}
    except requests.RequestException as e:
        return {"success": False, "message": f"Connection failed: {e}"}

    if response.status_code in _BLOCKED_STATUS_CODES or _CHALLENGE_MARKER in response.text:
        if _booknode_fallback_enabled():
            return {
                "success": True,
                "message": (
                    "Babelio serves its anti-bot captcha; searches will use Booknode instead"
                ),
            }
        return {
            "success": False,
            "message": "Babelio serves its anti-bot captcha; enable the Booknode fallback",
        }
    if response.ok:
        return {"success": True, "message": "Successfully connected to Babelio"}
    return {"success": False, "message": f"Babelio returned HTTP {response.status_code}"}


@register_settings("babelio", "Babelio", icon="library", order=57, group="metadata_providers")
def babelio_settings() -> list[SettingsField]:
    """Babelio metadata provider settings."""
    return [
        HeadingField(
            key="babelio_heading",
            title="Babelio",
            description=(
                "French reader community catalog. No API key required. Babelio shows an "
                "anti-bot captcha to non-browser clients, so it often returns nothing; "
                "with the Booknode fallback on, those searches use Booknode instead."
            ),
            link_url=BABELIO_BASE_URL,
            link_text="babelio.com",
        ),
        CheckboxField(
            key="BABELIO_ENABLED",
            label="Enable Babelio",
            description="Enable Babelio as a metadata provider for book searches",
            default=False,
        ),
        CheckboxField(
            key="BABELIO_BOOKNODE_FALLBACK",
            label="Fall back to Booknode",
            description=(
                "When Babelio is blocked or finds nothing, search Booknode instead. "
                "Works even if the Booknode provider itself is not enabled."
            ),
            default=True,
        ),
        ActionButton(
            key="test_connection",
            label="Test Connection",
            description="Check whether Babelio answers searches",
            style="primary",
            callback=_test_babelio_connection,
        ),
    ]
