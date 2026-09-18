"""Libgen metadata provider - adapts the Libgen release source for Direct-mode discovery.

Registered as "libgen_search" (not "libgen", which is the unrelated ReleaseSource name
under shelfmark.release_sources.libgen.source) so it joins the Direct-mode metadata
fallback fan-out driven by shelfmark.core.metadata_orchestrator.discover_books, alongside
Open Library, Google Books, Hardcover and Moly. See docs/direct-mode-fallback.md.

The name is chosen deliberately: shelfmark.metadata_providers.is_provider_enabled()
derives its config key as f"{name.upper()}_ENABLED", and "libgen_search".upper() is
"LIBGEN_SEARCH", which resolves to LIBGEN_SEARCH_ENABLED - the exact flag already
registered by shelfmark.release_sources.libgen.settings for Universal-mode search. One
checkbox controls both modes; there is no separate "enable for Direct mode" setting.

No network or parsing code lives here. shelfmark.release_sources.libgen.scraper already
does the real work (plain HTTP GET against user-configured mirrors - "not behind
DDoS-Guard, so no browser/bypasser is needed", per its own docstring); this module only
reshapes its BrowseRecord results into BookMetadata so they can flow through the same
orchestrator/dedup pipeline as the bibliographic providers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from shelfmark.core.config import config
from shelfmark.core.mirrors import get_libgen_mirrors, has_libgen_mirror_configuration
from shelfmark.metadata_providers import (
    BookMetadata,
    MetadataProvider,
    MetadataSearchOptions,
    register_provider,
)
from shelfmark.release_sources.libgen import scraper

if TYPE_CHECKING:
    from shelfmark.release_sources import BrowseRecord

_DEFAULT_MAX_RESULTS = 25


def _coerce_positive_int(value: object, default: int) -> int:
    """Same guard as shelfmark.release_sources.libgen.source._coerce_positive_int."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value > 0:
        return value
    return default


def _record_to_book(record: BrowseRecord) -> BookMetadata:
    """Convert a Libgen BrowseRecord into BookMetadata.

    `record.id` is already the bare md5 (see scraper._parse_results /
    _parse_ads_metadata). It is copied into both `provider_id` (Libgen's own natural key
    for a record) and `md5`: the latter lets metadata_dedup.merge_results match this
    against an Anna's Archive result for the same file, and lets a later direct download
    key off it - but it is never required for this card to display or be selected (see
    BookMetadata.md5's docstring).
    """
    return BookMetadata(
        provider="libgen_search",
        provider_id=record.id,
        title=record.title,
        provider_display_name="Libgen",
        authors=[record.author] if record.author else [],
        language=record.language,
        md5=record.id,
        source_url=record.source_url,
    )


@register_provider("libgen_search")
class LibgenSearchProvider(MetadataProvider):
    """Direct-mode discovery adapter over the Libgen catalogue.

    Delegates every network call to shelfmark.release_sources.libgen.scraper - the same
    module the "libgen" ReleaseSource uses for Universal-mode search - so there is exactly
    one implementation of Libgen fetch/parse behavior in the codebase.
    """

    name = "libgen_search"
    display_name = "Libgen"
    requires_auth = False

    def is_available(self) -> bool:
        """Available when at least one Libgen mirror is configured.

        The LIBGEN_SEARCH_ENABLED checkbox itself is checked by
        shelfmark.metadata_providers.is_provider_enabled("libgen_search") before this
        provider is even instantiated - see the module docstring.
        """
        return has_libgen_mirror_configuration()

    def search(self, options: MetadataSearchOptions) -> list[BookMetadata]:
        """Search the Libgen catalogue for discovery purposes.

        Deliberately does not catch exceptions from `scraper.search_libgen`: a genuine
        failure should surface as this provider's status being "error" rather than being
        silently swallowed into "no results" (shelfmark.core.metadata_orchestrator
        isolates it from every other provider either way - see _run_one_provider).
        Network-level failures (a mirror being unreachable) are already handled inside
        scraper.fetch_page, which returns None rather than raising.
        """
        query = options.query.strip()
        if not query:
            return []

        configured_max = _coerce_positive_int(
            config.get("LIBGEN_SEARCH_MAX_RESULTS", _DEFAULT_MAX_RESULTS), _DEFAULT_MAX_RESULTS
        )
        max_results = min(configured_max, options.limit) if options.limit else configured_max

        records = scraper.search_libgen(query, get_libgen_mirrors(), max_results=max_results)
        return [_record_to_book(record) for record in records]

    def get_book(self, book_id: str) -> BookMetadata | None:
        """Resolve a single Libgen record by its md5 (the id search() returns)."""
        record = scraper.fetch_record_by_md5(book_id, get_libgen_mirrors())
        return _record_to_book(record) if record else None

    def search_by_isbn(self, isbn: str) -> BookMetadata | None:
        """Libgen's catalogue search accepts an ISBN as free text - no dedicated endpoint."""
        books = self.search(MetadataSearchOptions(query=isbn, limit=1))
        return books[0] if books else None
