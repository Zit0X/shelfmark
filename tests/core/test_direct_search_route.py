"""Tests for GET /api/direct-search - the Direct-mode metadata-provider fallback.

Anna's Archive (via the Direct Download source) is mocked exactly like the existing
`/api/releases` direct-provider tests; the metadata-provider fan-out is mocked at
`metadata_orchestrator.discover_books` so these tests exercise the route's merging and
status-reporting logic without hitting real providers or the network.
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest

from shelfmark.core.metadata_orchestrator import DiscoveryResult, ProviderStatus
from shelfmark.metadata_providers import BookMetadata
from shelfmark.release_sources import Release, SourceUnavailableError

_ENABLED_CONFIG = {
    "DIRECT_MODE_METADATA_FALLBACK_ENABLED": True,
    "DIRECT_MODE_FALLBACK_STRATEGY": "on_empty_or_error",
    "DIRECT_MODE_METADATA_TIMEOUT_SECONDS": 8,
    "DIRECT_MODE_METADATA_MAX_RESULTS_PER_PROVIDER": 20,
}


@pytest.fixture(scope="module")
def main_module():
    with patch("shelfmark.download.orchestrator.start"):
        import shelfmark.main as main

        importlib.reload(main)
        return main


@pytest.fixture
def client(main_module):
    return main_module.app.test_client()


def _authenticate(client) -> None:
    with client.session_transaction() as sess:
        sess["user_id"] = "alice"
        sess["is_admin"] = False
        sess["db_user_id"] = 7


def _config_get(overrides):
    def _get(key, default=None, **_kw):
        return overrides.get(key, default)

    return _get


class _FailingDirectSource:
    def search(self, book, plan, expand_search=False, content_type="ebook"):
        raise SourceUnavailableError("Anna's Archive is unreachable")


class _EmptyDirectSource:
    def search(self, book, plan, expand_search=False, content_type="ebook"):
        return []


class _WorkingDirectSource:
    def __init__(self, releases):
        self._releases = releases

    def search(self, book, plan, expand_search=False, content_type="ebook"):
        return list(self._releases)


def test_returns_404_when_flag_is_disabled(main_module, client):
    _authenticate(client)
    with (
        patch.object(main_module, "get_auth_mode", return_value="none"),
        patch.object(main_module.app_config, "get", side_effect=_config_get({})),
        patch(
            "shelfmark.core.metadata_orchestrator.discover_books",
            side_effect=AssertionError("must not run when the flag is off"),
        ),
    ):
        resp = client.get("/api/direct-search", query_string={"query": "Dune"})

    assert resp.status_code == 404


def test_metadata_provider_answers_when_anna_archive_fails(main_module, client):
    """Acceptance case: AA down, Open Library still finds the book."""
    _authenticate(client)
    discovery = DiscoveryResult(
        books=[
            BookMetadata(
                provider="openlibrary",
                provider_id="OL1W",
                provider_display_name="Open Library",
                title="Meurtre au festival des citrouilles",
                authors=["Ava Manceau"],
            )
        ],
        provider_statuses=[
            ProviderStatus(name="openlibrary", display_name="Open Library", status="ok", count=1)
        ],
    )

    with (
        patch.object(main_module, "get_auth_mode", return_value="none"),
        patch.object(main_module.app_config, "get", side_effect=_config_get(_ENABLED_CONFIG)),
        patch("shelfmark.release_sources.get_source", return_value=_FailingDirectSource()),
        patch("shelfmark.core.metadata_orchestrator.discover_books", return_value=discovery),
    ):
        resp = client.get(
            "/api/direct-search", query_string={"query": "Meurtre au festival des citrouilles"}
        )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["releases"] == []
    assert len(body["discovery"]) == 1
    assert body["discovery"][0]["title"] == "Meurtre au festival des citrouilles"
    assert body["discovery"][0]["sources"][0]["provider"] == "openlibrary"
    statuses = {s["name"]: s for s in body["provider_statuses"]}
    assert statuses["direct_download"]["status"] == "error"
    assert statuses["openlibrary"]["status"] == "ok"


def test_libgen_answers_when_aa_and_bibliographic_providers_all_miss(main_module, client):
    """Reproduction case: AA fails, Open Library/Google Books/Hardcover know nothing,
    but Libgen (searched independently, not via an AA md5) has the book.
    """
    _authenticate(client)
    discovery = DiscoveryResult(
        books=[
            BookMetadata(
                provider="libgen_search",
                provider_id="d41d8cd98f00b204e9800998ecf8427e",
                provider_display_name="Libgen",
                title="Meurtre au festival des citrouilles",
                authors=["Ava Manceau"],
                language="fr",
                md5="d41d8cd98f00b204e9800998ecf8427e",
            )
        ],
        provider_statuses=[
            ProviderStatus(name="openlibrary", display_name="Open Library", status="no_results"),
            ProviderStatus(name="googlebooks", display_name="Google Books", status="no_results"),
            ProviderStatus(name="hardcover", display_name="Hardcover", status="no_results"),
            ProviderStatus(name="libgen_search", display_name="Libgen", status="ok", count=1),
        ],
    )

    with (
        patch.object(main_module, "get_auth_mode", return_value="none"),
        patch.object(main_module.app_config, "get", side_effect=_config_get(_ENABLED_CONFIG)),
        patch("shelfmark.release_sources.get_source", return_value=_FailingDirectSource()),
        patch("shelfmark.core.metadata_orchestrator.discover_books", return_value=discovery),
    ):
        resp = client.get(
            "/api/direct-search",
            query_string={"query": "Meurtre au festival des citrouilles Ava Manceau"},
        )

    assert resp.status_code == 200
    body = resp.get_json()

    # No downloadable release yet (Libgen only contributed a discovery card here) but the
    # card itself must still be present and fully usable.
    assert body["releases"] == []
    assert len(body["discovery"]) == 1

    card = body["discovery"][0]
    assert card["title"] == "Meurtre au festival des citrouilles"
    assert card["authors"] == ["Ava Manceau"]
    assert [s["provider"] for s in card["sources"]] == ["libgen_search"]
    # The absence of an Anna's Archive md5 does not block the card: it carries its own.
    assert card["md5"] == "d41d8cd98f00b204e9800998ecf8427e"
    assert "annas_archive" not in {s["provider"] for s in card["sources"]}

    statuses = {s["name"]: s for s in body["provider_statuses"]}
    assert statuses["direct_download"]["status"] == "error"
    assert statuses["openlibrary"]["status"] == "no_results"
    assert statuses["googlebooks"]["status"] == "no_results"
    assert statuses["hardcover"]["status"] == "no_results"
    assert statuses["libgen_search"]["status"] == "ok"
    assert statuses["libgen_search"]["count"] == 1


def test_matching_result_from_both_sources_is_shown_once(main_module, client):
    release = Release(
        source="direct_download",
        source_id="aa-md5",
        title="Dune",
        format="epub",
        extra={"author": "Frank Herbert"},
    )
    discovery = DiscoveryResult(
        books=[
            BookMetadata(
                provider="openlibrary", provider_id="OL1W", title="Dune", authors=["Frank Herbert"]
            )
        ],
        provider_statuses=[
            ProviderStatus(name="openlibrary", display_name="Open Library", status="ok", count=1)
        ],
    )

    with (
        patch.object(main_module, "get_auth_mode", return_value="none"),
        patch.object(
            main_module.app_config,
            "get",
            side_effect=_config_get(
                {**_ENABLED_CONFIG, "DIRECT_MODE_FALLBACK_STRATEGY": "parallel"}
            ),
        ),
        patch("shelfmark.release_sources.get_source", return_value=_WorkingDirectSource([release])),
        patch("shelfmark.core.metadata_orchestrator.discover_books", return_value=discovery),
    ):
        resp = client.get("/api/direct-search", query_string={"query": "Dune"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body["releases"]) == 1
    assert body["discovery"] == []


def test_no_provider_responds_returns_200_with_statuses(main_module, client):
    discovery = DiscoveryResult(
        books=[],
        provider_statuses=[
            ProviderStatus(name="openlibrary", display_name="Open Library", status="no_results"),
            ProviderStatus(
                name="googlebooks", display_name="Google Books", status="error", message="Timed out"
            ),
        ],
    )

    with (
        patch.object(main_module, "get_auth_mode", return_value="none"),
        patch.object(main_module.app_config, "get", side_effect=_config_get(_ENABLED_CONFIG)),
        patch("shelfmark.release_sources.get_source", return_value=_EmptyDirectSource()),
        patch("shelfmark.core.metadata_orchestrator.discover_books", return_value=discovery),
    ):
        resp = client.get("/api/direct-search", query_string={"query": "Some Unknown Book"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["releases"] == []
    assert body["discovery"] == []
    statuses = {s["name"]: s for s in body["provider_statuses"]}
    assert statuses["direct_download"]["status"] == "no_results"
    assert statuses["openlibrary"]["status"] == "no_results"
    assert statuses["googlebooks"]["status"] == "error"


def test_on_empty_or_error_strategy_skips_metadata_when_direct_download_succeeds(
    main_module, client
):
    release = Release(source="direct_download", source_id="aa-md5", title="Dune", format="epub")

    with (
        patch.object(main_module, "get_auth_mode", return_value="none"),
        patch.object(main_module.app_config, "get", side_effect=_config_get(_ENABLED_CONFIG)),
        patch("shelfmark.release_sources.get_source", return_value=_WorkingDirectSource([release])),
        patch(
            "shelfmark.core.metadata_orchestrator.discover_books",
            side_effect=AssertionError("metadata providers should not be queried"),
        ),
    ):
        resp = client.get("/api/direct-search", query_string={"query": "Dune"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body["releases"]) == 1


def test_parallel_strategy_includes_distinct_libgen_book_alongside_generic_aa_results(
    main_module, client
):
    """Reproduction case: Anna's Archive returns non-empty but generic/unrelated results,
    and with strategy="parallel" Libgen is still queried independently and contributes a
    distinct book that must appear in the final response, unmerged with the AA results.
    """
    _authenticate(client)
    generic_aa_releases = [
        Release(
            source="direct_download",
            source_id="aa-md5-1",
            title="Some Other Mystery",
            format="epub",
        ),
        Release(
            source="direct_download",
            source_id="aa-md5-2",
            title="Another Unrelated Book",
            format="epub",
        ),
    ]
    discovery = DiscoveryResult(
        books=[
            BookMetadata(
                provider="libgen_search",
                provider_id="d41d8cd98f00b204e9800998ecf8427e",
                provider_display_name="Libgen",
                title="Meurtre au festival des citrouilles",
                authors=["Ava Manceau"],
                language="fr",
                md5="d41d8cd98f00b204e9800998ecf8427e",
                series_name="Les mystères de Little Bramble",
                series_position=1,
            )
        ],
        provider_statuses=[
            ProviderStatus(name="openlibrary", display_name="Open Library", status="no_results"),
            ProviderStatus(name="googlebooks", display_name="Google Books", status="no_results"),
            ProviderStatus(name="libgen_search", display_name="Libgen", status="ok", count=1),
        ],
    )

    with (
        patch.object(main_module, "get_auth_mode", return_value="none"),
        patch.object(
            main_module.app_config,
            "get",
            side_effect=_config_get(
                {**_ENABLED_CONFIG, "DIRECT_MODE_FALLBACK_STRATEGY": "parallel"}
            ),
        ),
        patch(
            "shelfmark.release_sources.get_source",
            return_value=_WorkingDirectSource(generic_aa_releases),
        ),
        patch("shelfmark.core.metadata_orchestrator.discover_books", return_value=discovery),
    ):
        resp = client.get(
            "/api/direct-search",
            query_string={"query": "Meurtre au festival des citrouilles Ava Manceau"},
        )

    assert resp.status_code == 200
    body = resp.get_json()

    # The two generic AA releases are preserved, untouched by the metadata fallback.
    assert len(body["releases"]) == 2
    assert {r["title"] for r in body["releases"]} == {
        "Some Other Mystery",
        "Another Unrelated Book",
    }

    # The distinct Libgen book is present alongside them, not merged away.
    assert len(body["discovery"]) == 1
    card = body["discovery"][0]
    assert card["title"] == "Meurtre au festival des citrouilles"
    assert card["authors"] == ["Ava Manceau"]
    assert card["language"] == "fr"
    assert card["series_name"] == "Les mystères de Little Bramble"
    assert card["series_position"] == 1
    assert [s["provider"] for s in card["sources"]] == ["libgen_search"]

    statuses = {s["name"]: s for s in body["provider_statuses"]}
    assert statuses["direct_download"]["status"] == "ok"
    assert statuses["direct_download"]["count"] == 2
    assert statuses["libgen_search"]["status"] == "ok"
    assert statuses["libgen_search"]["count"] == 1


def test_parallel_strategy_always_calls_discover_books_when_direct_download_succeeds(
    main_module, client
):
    """strategy="parallel" must query metadata providers (Libgen included) even when
    Anna's Archive already found results - the defining behavior distinguishing it from
    "on_empty_or_error".
    """
    release = Release(source="direct_download", source_id="aa-md5", title="Dune", format="epub")
    discovery = DiscoveryResult(books=[], provider_statuses=[])
    discover_books_mock = patch(
        "shelfmark.core.metadata_orchestrator.discover_books", return_value=discovery
    )

    with (
        patch.object(main_module, "get_auth_mode", return_value="none"),
        patch.object(
            main_module.app_config,
            "get",
            side_effect=_config_get(
                {**_ENABLED_CONFIG, "DIRECT_MODE_FALLBACK_STRATEGY": "parallel"}
            ),
        ),
        patch("shelfmark.release_sources.get_source", return_value=_WorkingDirectSource([release])),
        discover_books_mock as mocked_discover_books,
    ):
        resp = client.get("/api/direct-search", query_string={"query": "Dune"})

    assert resp.status_code == 200
    mocked_discover_books.assert_called_once()
