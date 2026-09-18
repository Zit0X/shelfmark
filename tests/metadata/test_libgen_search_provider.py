"""Tests for LibgenSearchProvider: the Direct-mode discovery adapter over Libgen.

Network access is mocked at the same seam tests/libgen/test_source.py uses
(scraper.search_libgen / scraper.fetch_record_by_md5) - no real HTTP call is ever made.
"""

from unittest.mock import patch

import shelfmark.core.metadata_orchestrator as orchestrator
import shelfmark.metadata_providers.libgen_search as libgen_search_module
from shelfmark.metadata_providers import (
    BookMetadata,
    MetadataSearchOptions,
    is_provider_enabled,
)
from shelfmark.metadata_providers.libgen_search import LibgenSearchProvider, _record_to_book
from shelfmark.release_sources import BrowseRecord

MD5 = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1"


def _record(**overrides):
    fields = {
        "id": MD5,
        "title": "Meurtre au festival des citrouilles",
        "source": "libgen",
        "author": "Ava Manceau",
        "language": "fr",
        "format": "epub",
        "size": "1.2 MB",
        "source_url": "https://libgen.example/ads.php?md5=" + MD5,
    }
    fields.update(overrides)
    return BrowseRecord(**fields)


class TestRecordToBook:
    def test_maps_fields(self):
        book = _record_to_book(_record())

        assert book.provider == "libgen_search"
        assert book.provider_display_name == "Libgen"
        assert book.provider_id == MD5
        assert book.md5 == MD5
        assert book.title == "Meurtre au festival des citrouilles"
        assert book.authors == ["Ava Manceau"]
        assert book.language == "fr"
        assert book.source_url.endswith(MD5)

    def test_no_author_yields_empty_authors_list(self):
        book = _record_to_book(_record(author=None))
        assert book.authors == []


class TestIsAvailable:
    def test_available_with_mirrors(self):
        with patch.object(
            libgen_search_module, "has_libgen_mirror_configuration", return_value=True
        ):
            assert LibgenSearchProvider().is_available() is True

    def test_unavailable_without_mirrors(self):
        with patch.object(
            libgen_search_module, "has_libgen_mirror_configuration", return_value=False
        ):
            assert LibgenSearchProvider().is_available() is False


class TestEnabledFlagSharing:
    """LIBGEN_SEARCH_ENABLED must gate both Universal-mode search and this provider."""

    def test_is_provider_enabled_reflects_libgen_search_enabled(self):
        from shelfmark.core.config import config as app_config

        with patch.object(
            app_config, "get", side_effect=lambda k, d=None, **_kw: k == "LIBGEN_SEARCH_ENABLED"
        ):
            assert is_provider_enabled("libgen_search") is True

    def test_is_provider_enabled_false_when_flag_off(self):
        from shelfmark.core.config import config as app_config

        with patch.object(app_config, "get", side_effect=lambda k, d=None, **_kw: False):
            assert is_provider_enabled("libgen_search") is False


class TestSearch:
    def test_returns_books_from_scraper_records(self):
        with (
            patch.object(libgen_search_module, "get_libgen_mirrors", return_value=["https://x"]),
            patch.object(libgen_search_module.config, "get", side_effect=lambda k, d=None: d),
            patch.object(
                libgen_search_module.scraper, "search_libgen", return_value=[_record()]
            ) as mock_search,
        ):
            books = LibgenSearchProvider().search(
                MetadataSearchOptions(query="Meurtre au festival des citrouilles")
            )

        assert len(books) == 1
        assert books[0].provider == "libgen_search"
        assert books[0].title == "Meurtre au festival des citrouilles"
        mock_search.assert_called_once()

    def test_empty_query_returns_empty_without_calling_scraper(self):
        with patch.object(libgen_search_module.scraper, "search_libgen") as mock_search:
            books = LibgenSearchProvider().search(MetadataSearchOptions(query="   "))

        assert books == []
        mock_search.assert_not_called()

    def test_max_results_defaults_and_respects_options_limit(self):
        with (
            patch.object(libgen_search_module, "get_libgen_mirrors", return_value=["https://x"]),
            patch.object(libgen_search_module.config, "get", side_effect=lambda k, d=None: d),
            patch.object(
                libgen_search_module.scraper, "search_libgen", return_value=[]
            ) as mock_search,
        ):
            LibgenSearchProvider().search(MetadataSearchOptions(query="Dune", limit=5))

        _, kwargs = mock_search.call_args
        assert kwargs["max_results"] == 5

    def test_no_results_returns_empty_list(self):
        with (
            patch.object(libgen_search_module, "get_libgen_mirrors", return_value=["https://x"]),
            patch.object(libgen_search_module.config, "get", side_effect=lambda k, d=None: d),
            patch.object(libgen_search_module.scraper, "search_libgen", return_value=[]),
        ):
            assert LibgenSearchProvider().search(MetadataSearchOptions(query="Dune")) == []

    def test_scraper_error_is_not_swallowed(self):
        """A genuine failure surfaces as this provider's own error, not silent no-results.

        shelfmark.core.metadata_orchestrator isolates it from other providers either way
        (see TestErrorIsolation below) - this only proves the provider does not itself
        mask the failure as "no results".
        """
        with (
            patch.object(libgen_search_module, "get_libgen_mirrors", return_value=["https://x"]),
            patch.object(libgen_search_module.config, "get", side_effect=lambda k, d=None: d),
            patch.object(
                libgen_search_module.scraper,
                "search_libgen",
                side_effect=RuntimeError("mirror unreachable"),
            ),
        ):
            try:
                LibgenSearchProvider().search(MetadataSearchOptions(query="Dune"))
            except RuntimeError as exc:
                assert "mirror unreachable" in str(exc)
            else:
                raise AssertionError("expected RuntimeError to propagate")


class TestGetBook:
    def test_resolves_by_md5(self):
        with patch.object(
            libgen_search_module.scraper, "fetch_record_by_md5", return_value=_record()
        ):
            book = LibgenSearchProvider().get_book(MD5)

        assert book is not None
        assert book.md5 == MD5

    def test_returns_none_when_not_found(self):
        with patch.object(libgen_search_module.scraper, "fetch_record_by_md5", return_value=None):
            assert LibgenSearchProvider().get_book(MD5) is None


class TestSearchByIsbn:
    def test_delegates_to_search(self):
        with (
            patch.object(libgen_search_module, "get_libgen_mirrors", return_value=["https://x"]),
            patch.object(libgen_search_module.config, "get", side_effect=lambda k, d=None: d),
            patch.object(libgen_search_module.scraper, "search_libgen", return_value=[_record()]),
        ):
            book = LibgenSearchProvider().search_by_isbn("9780441013593")

        assert book is not None
        assert book.md5 == MD5

    def test_no_match_returns_none(self):
        with (
            patch.object(libgen_search_module, "get_libgen_mirrors", return_value=["https://x"]),
            patch.object(libgen_search_module.config, "get", side_effect=lambda k, d=None: d),
            patch.object(libgen_search_module.scraper, "search_libgen", return_value=[]),
        ):
            assert LibgenSearchProvider().search_by_isbn("9780441013593") is None


class _FakeOpenLibrary:
    name = "openlibrary"
    display_name = "Open Library"

    def is_available(self):
        return True

    def search(self, options):
        return [BookMetadata(provider="openlibrary", provider_id="OL1W", title="Dune")]


class TestErrorIsolation:
    """Proves the *real*, registered LibgenSearchProvider is isolated by the orchestrator.

    shelfmark.core.metadata_orchestrator's fault isolation is already covered generically
    in tests/core/test_metadata_orchestrator.py; this test additionally proves that the
    real Libgen adapter (not a stand-in) is wired into that isolation correctly.
    """

    def test_libgen_error_does_not_block_other_providers(self, monkeypatch):
        real_libgen = LibgenSearchProvider()
        fake_openlibrary = _FakeOpenLibrary()

        monkeypatch.setattr(
            orchestrator,
            "list_providers",
            lambda: [
                {"name": "libgen_search", "display_name": "Libgen", "requires_auth": False},
                {"name": "openlibrary", "display_name": "Open Library", "requires_auth": False},
            ],
        )
        monkeypatch.setattr(orchestrator, "is_provider_enabled", lambda name: True)
        monkeypatch.setattr(orchestrator, "get_provider_kwargs", lambda name: {})
        monkeypatch.setattr(
            orchestrator,
            "get_provider",
            lambda name, **kw: real_libgen if name == "libgen_search" else fake_openlibrary,
        )
        monkeypatch.setattr(libgen_search_module, "has_libgen_mirror_configuration", lambda: True)
        monkeypatch.setattr(
            libgen_search_module.scraper,
            "search_libgen",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("mirror down")),
        )

        from shelfmark.core.query_normalization import QueryStep

        result = orchestrator.discover_books(
            [QueryStep(kind="title", query="Dune", normalized="dune")]
        )

        assert len(result.books) == 1
        assert result.books[0].provider == "openlibrary"
        statuses = {s.name: s for s in result.provider_statuses}
        assert statuses["libgen_search"].status == "error"
        assert "mirror down" in statuses["libgen_search"].message
        assert statuses["openlibrary"].status == "ok"
