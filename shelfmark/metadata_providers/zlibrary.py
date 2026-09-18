"""Z-Library metadata provider - intentionally non-functional placeholder.

Z-Library has no publicly documented, ToS-compatible search API, and its web catalogue
sits behind anti-bot protection. This project's compliance policy is that shelfmark does
not implement CAPTCHA, Cloudflare, DDoS-Guard, rate-limit, or any other anti-bot bypass -
not in the download path, and not here in the search path either.

This class exists only so the MetadataProvider interface has an explicit, documented
placeholder for Z-Library rather than silent absence. `is_available()` always returns
False, so shelfmark.core.metadata_orchestrator.discover_books never calls search(),
get_book() or search_by_isbn() on it in practice - the Direct-mode discovery fan-out
reports it the same way it reports any other provider that is not ready to use. No
settings entry is registered for it: a checkbox that can never turn this provider on
would be misleading UI.

This is unrelated to the existing "Z-Library mirror" setting (shelfmark.core.mirrors /
the direct-download mirror configuration) and the "zlib" partner-domain handling in
shelfmark.release_sources.direct_download.annas_archive - those resolve *downloads* for
an md5 Anna's Archive already found, which is a different concern from independently
*searching* Z-Library's own catalogue, which is what this module intentionally does not
implement.

If a legitimate, documented integration path for Z-Library ever exists (an official API,
or an arrangement comparable to how Anna's Archive is already used), replace this stub
following the same shape as shelfmark.metadata_providers.libgen_search - reusing an
existing, ToS-respecting fetch layer rather than adding a new one here.
"""

from __future__ import annotations

from shelfmark.metadata_providers import (
    BookMetadata,
    MetadataProvider,
    MetadataSearchOptions,
    register_provider,
)


@register_provider("zlibrary_search")
class ZLibrarySearchProvider(MetadataProvider):
    """Non-functional placeholder - see module docstring for why."""

    name = "zlibrary_search"
    display_name = "Z-Library"
    requires_auth = False

    def is_available(self) -> bool:
        """Always False. No compliant Z-Library search integration exists - see module docstring."""
        return False

    def search(self, options: MetadataSearchOptions) -> list[BookMetadata]:
        """Never called in practice (is_available() is always False, and never should be)."""
        return []

    def get_book(self, book_id: str) -> BookMetadata | None:
        """Never called in practice (is_available() is always False, and never should be)."""
        return None

    def search_by_isbn(self, isbn: str) -> BookMetadata | None:
        """Never called in practice (is_available() is always False, and never should be)."""
        return None
