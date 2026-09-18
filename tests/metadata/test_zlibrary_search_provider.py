"""Tests for ZLibrarySearchProvider: proves the stub never performs network access.

Z-Library has no compliant search integration in this project (see the module docstring
in shelfmark/metadata_providers/zlibrary.py). These tests assert the provider is
permanently inert, not merely "unconfigured by default".
"""

from unittest.mock import patch

import requests

import shelfmark.core.metadata_orchestrator as orchestrator
from shelfmark.metadata_providers import MetadataSearchOptions, is_provider_enabled
from shelfmark.metadata_providers.zlibrary import ZLibrarySearchProvider


def _network_call_forbidden(*_args, **_kwargs):
    raise AssertionError("ZLibrarySearchProvider must never perform a network call")


class TestNeverPerformsNetworkCalls:
    def test_search_makes_no_request(self):
        with patch.object(requests, "get", side_effect=_network_call_forbidden):
            books = ZLibrarySearchProvider().search(MetadataSearchOptions(query="Dune"))
        assert books == []

    def test_get_book_makes_no_request(self):
        with patch.object(requests, "get", side_effect=_network_call_forbidden):
            assert ZLibrarySearchProvider().get_book("some-id") is None

    def test_search_by_isbn_makes_no_request(self):
        with patch.object(requests, "get", side_effect=_network_call_forbidden):
            assert ZLibrarySearchProvider().search_by_isbn("9780441013593") is None


class TestAlwaysUnavailable:
    def test_is_available_is_false(self):
        assert ZLibrarySearchProvider().is_available() is False

    def test_is_available_is_false_even_if_enabled_flag_is_manually_forced_true(self):
        """Belt-and-suspenders: even a raw config-file edit cannot activate this provider."""
        from shelfmark.core.config import config as app_config

        with patch.object(
            app_config, "get", side_effect=lambda k, d=None, **_kw: k == "ZLIBRARY_SEARCH_ENABLED"
        ):
            assert is_provider_enabled("zlibrary_search") is True  # the flag gate opens...
            assert ZLibrarySearchProvider().is_available() is False  # ...but this stays shut


class TestDiscoveryNeverInvokesIt:
    def test_discover_books_never_calls_search_even_when_enabled(self, monkeypatch):
        provider = ZLibrarySearchProvider()
        with patch.object(provider, "search") as mock_search:
            monkeypatch.setattr(
                orchestrator,
                "list_providers",
                lambda: [
                    {"name": "zlibrary_search", "display_name": "Z-Library", "requires_auth": False}
                ],
            )
            # Simulate someone forcing the enabled flag on directly in a config file.
            monkeypatch.setattr(orchestrator, "is_provider_enabled", lambda name: True)
            monkeypatch.setattr(orchestrator, "get_provider_kwargs", lambda name: {})
            monkeypatch.setattr(orchestrator, "get_provider", lambda name, **kw: provider)

            from shelfmark.core.query_normalization import QueryStep

            result = orchestrator.discover_books(
                [QueryStep(kind="title", query="Dune", normalized="dune")]
            )

        mock_search.assert_not_called()
        assert result.books == []
        assert result.provider_statuses[0].status == "not_configured"


class TestNoSettingsExposed:
    def test_registered_under_zlibrary_search_name(self):
        from shelfmark.metadata_providers import is_provider_registered

        assert is_provider_registered("zlibrary_search")
        assert ZLibrarySearchProvider.display_name == "Z-Library"
