"""Tests for the parallel, fault-isolated multi-provider discovery orchestrator."""

import threading
import time

import shelfmark.core.metadata_orchestrator as orchestrator
from shelfmark.core.query_normalization import QueryStep
from shelfmark.metadata_providers import BookMetadata


class _FakeProvider:
    """Stand-in for a `MetadataProvider` - avoids touching the real registry/network."""

    def __init__(
        self,
        name,
        *,
        display_name=None,
        available=True,
        books=None,
        error=None,
        delay=0.0,
        concurrency_tracker=None,
    ):
        self.name = name
        self.display_name = display_name or name.title()
        self._available = available
        self._books = books or []
        self._error = error
        self._delay = delay
        self._concurrency_tracker = concurrency_tracker
        self.call_count = 0
        self.received_options = []

    def is_available(self):
        return self._available

    def search(self, options):
        self.call_count += 1
        self.received_options.append(options)
        if self._concurrency_tracker is not None:
            self._concurrency_tracker.enter()
        try:
            if self._delay:
                time.sleep(self._delay)
            if self._error:
                raise self._error
            return list(self._books)
        finally:
            if self._concurrency_tracker is not None:
                self._concurrency_tracker.exit()


class _ConcurrencyTracker:
    """Tracks the peak number of simultaneously-running `search()` calls."""

    def __init__(self):
        self._lock = threading.Lock()
        self._current = 0
        self.peak = 0

    def enter(self):
        with self._lock:
            self._current += 1
            self.peak = max(self.peak, self._current)

    def exit(self):
        with self._lock:
            self._current -= 1


def _install_fake_providers(monkeypatch, providers, enabled_names=None):
    enabled = enabled_names if enabled_names is not None else {p.name for p in providers}
    by_name = {p.name: p for p in providers}

    monkeypatch.setattr(
        orchestrator,
        "list_providers",
        lambda: [
            {"name": p.name, "display_name": p.display_name, "requires_auth": False}
            for p in providers
        ],
    )
    monkeypatch.setattr(orchestrator, "is_provider_enabled", lambda name: name in enabled)
    monkeypatch.setattr(orchestrator, "get_provider_kwargs", lambda name: {})
    monkeypatch.setattr(orchestrator, "get_provider", lambda name, **kwargs: by_name[name])
    return by_name


def _steps(*queries):
    return [QueryStep(kind="title", query=q, normalized=q.lower()) for q in queries]


def _book(provider, title="Dune"):
    return BookMetadata(provider=provider, provider_id="1", title=title)


class TestProviderResolution:
    def test_disabled_provider_is_not_queried(self, monkeypatch):
        provider = _FakeProvider("openlibrary", books=[_book("openlibrary")])
        _install_fake_providers(monkeypatch, [provider], enabled_names=set())

        result = orchestrator.discover_books(_steps("Dune"))

        assert provider.call_count == 0
        assert result.books == []
        status = result.provider_statuses[0]
        assert status.status == "disabled"

    def test_enabled_but_unconfigured_provider_is_not_queried(self, monkeypatch):
        provider = _FakeProvider("googlebooks", available=False)
        _install_fake_providers(monkeypatch, [provider])

        result = orchestrator.discover_books(_steps("Dune"))

        assert provider.call_count == 0
        status = result.provider_statuses[0]
        assert status.status == "not_configured"

    def test_provider_with_no_results_is_reported(self, monkeypatch):
        provider = _FakeProvider("openlibrary", books=[])
        _install_fake_providers(monkeypatch, [provider])

        result = orchestrator.discover_books(_steps("Some Obscure Title"))

        assert provider.call_count == 1
        status = result.provider_statuses[0]
        assert status.status == "no_results"
        assert status.count == 0


class TestFaultIsolation:
    def test_one_failing_provider_does_not_block_the_others(self, monkeypatch):
        failing = _FakeProvider("googlebooks", error=RuntimeError("boom"))
        working = _FakeProvider("openlibrary", books=[_book("openlibrary")])
        _install_fake_providers(monkeypatch, [failing, working])

        result = orchestrator.discover_books(_steps("Dune"))

        assert len(result.books) == 1
        statuses = {s.name: s for s in result.provider_statuses}
        assert statuses["googlebooks"].status == "error"
        assert "boom" in statuses["googlebooks"].message
        assert statuses["openlibrary"].status == "ok"

    def test_slow_provider_is_reported_as_timed_out_without_blocking_result(self, monkeypatch):
        slow = _FakeProvider("googlebooks", delay=0.5, books=[_book("googlebooks")])
        fast = _FakeProvider("openlibrary", books=[_book("openlibrary")])
        _install_fake_providers(monkeypatch, [slow, fast])

        started = time.monotonic()
        result = orchestrator.discover_books(_steps("Dune"), timeout_seconds=0.05)
        elapsed = time.monotonic() - started

        # The call returns close to the timeout, not after the slow provider's delay.
        assert elapsed < 0.4
        assert len(result.books) == 1
        assert result.books[0].provider == "openlibrary"
        statuses = {s.name: s for s in result.provider_statuses}
        assert statuses["googlebooks"].status == "error"
        assert "Timed out" in statuses["googlebooks"].message
        assert statuses["openlibrary"].status == "ok"

    def test_all_providers_failing_returns_empty_without_raising(self, monkeypatch):
        a = _FakeProvider("openlibrary", error=RuntimeError("a"))
        b = _FakeProvider("googlebooks", error=RuntimeError("b"))
        _install_fake_providers(monkeypatch, [a, b])

        result = orchestrator.discover_books(_steps("Dune"))

        assert result.books == []
        assert all(status.status == "error" for status in result.provider_statuses)


class TestConcurrencyLimit:
    def test_never_exceeds_max_workers(self, monkeypatch):
        tracker = _ConcurrencyTracker()
        providers = [
            _FakeProvider(f"provider{i}", delay=0.05, concurrency_tracker=tracker) for i in range(5)
        ]
        _install_fake_providers(monkeypatch, providers)

        orchestrator.discover_books(_steps("Dune"), max_workers=2)

        assert tracker.peak <= 2


class TestLanguageFilter:
    def test_language_is_forwarded_to_every_provider_call(self, monkeypatch):
        provider = _FakeProvider("openlibrary", books=[])
        _install_fake_providers(monkeypatch, [provider])

        orchestrator.discover_books(_steps("Dune"), language="fr")

        assert provider.received_options[0].language == "fr"

    def test_language_defaults_to_none(self, monkeypatch):
        provider = _FakeProvider("openlibrary", books=[])
        _install_fake_providers(monkeypatch, [provider])

        orchestrator.discover_books(_steps("Dune"))

        assert provider.received_options[0].language is None


class TestProgressiveSteps:
    def test_stops_once_enough_results_accumulated(self, monkeypatch):
        provider = _FakeProvider("openlibrary", books=[_book("openlibrary"), _book("openlibrary")])
        _install_fake_providers(monkeypatch, [provider])

        orchestrator.discover_books(
            _steps("title and author", "title only", "author only"),
            min_results_to_stop=2,
        )

        # Only the first step was needed to reach the threshold.
        assert provider.call_count == 1

    def test_tries_next_step_when_first_step_is_insufficient(self, monkeypatch):
        provider = _FakeProvider("openlibrary", books=[])
        _install_fake_providers(monkeypatch, [provider])

        orchestrator.discover_books(_steps("title and author", "title only"), min_results_to_stop=1)

        assert provider.call_count == 2

    def test_no_steps_returns_empty(self, monkeypatch):
        provider = _FakeProvider("openlibrary", books=[_book("openlibrary")])
        _install_fake_providers(monkeypatch, [provider])

        result = orchestrator.discover_books([])

        assert provider.call_count == 0
        assert result.books == []

    def test_no_active_providers_returns_reported_statuses(self, monkeypatch):
        _install_fake_providers(monkeypatch, [_FakeProvider("openlibrary")], enabled_names=set())

        result = orchestrator.discover_books(_steps("Dune"))

        assert result.books == []
        assert len(result.provider_statuses) == 1
        assert result.provider_statuses[0].status == "disabled"
