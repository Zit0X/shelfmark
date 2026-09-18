"""Parallel, fault-isolated fan-out across metadata (search) providers.

Drives `shelfmark.metadata_providers` (Open Library, Google Books, Hardcover, ...) for the
Direct-mode metadata fallback: several providers are queried at once, a slow or failing one
never blocks the others, and every provider gets a reported status so the UI can say why a
result is missing instead of silently returning nothing (see `shelfmark.core.metadata_dedup`
for how the raw results this returns get merged into one deduplicated list).

Caching and rate limiting stay exactly what each provider already does for itself
(`@cacheable` on `_search_cached`, Open Library's `RateLimiter`, etc.) - this module adds a
concurrency limit and a per-call timeout on top, nothing more.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from shelfmark.core.logger import setup_logger
from shelfmark.metadata_providers import (
    BookMetadata,
    MetadataSearchOptions,
    SearchType,
    get_provider,
    get_provider_kwargs,
    is_provider_enabled,
    list_providers,
)

if TYPE_CHECKING:
    from shelfmark.core.query_normalization import QueryStep
    from shelfmark.metadata_providers import MetadataProvider

logger = setup_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_LIMIT_PER_PROVIDER = 20
# Stop trying further progressive query steps once at least this many raw results have
# come back across all providers combined - exact deduplication happens later, in
# metadata_dedup, this is only a cheap "is it worth another step" heuristic.
DEFAULT_MIN_RESULTS_TO_STOP = 3
DEFAULT_MAX_WORKERS = 6

_STATUS_OK = "ok"
_STATUS_NO_RESULTS = "no_results"
_STATUS_ERROR = "error"
_STATUS_DISABLED = "disabled"
_STATUS_NOT_CONFIGURED = "not_configured"


@dataclass
class ProviderStatus:
    """What one metadata provider did during a discovery call, for the UI to report."""

    name: str
    display_name: str
    status: str  # "ok" | "no_results" | "error" | "disabled" | "not_configured"
    message: str | None = None
    count: int = 0
    elapsed_ms: int = 0


@dataclass
class DiscoveryResult:
    """Raw (not yet deduplicated) results from a multi-provider discovery call."""

    books: list[BookMetadata] = field(default_factory=list)
    provider_statuses: list[ProviderStatus] = field(default_factory=list)


def _build_search_options(
    step: QueryStep, *, limit: int, language: str | None
) -> MetadataSearchOptions:
    """Turn one progressive-search step into provider search options."""
    fields: dict[str, str] = {}
    search_type = SearchType.GENERAL

    if step.kind == "title_author":
        if step.title:
            fields["title"] = step.title
        if step.author:
            fields["author"] = step.author
    elif step.kind == "title":
        search_type = SearchType.TITLE
    elif step.kind == "author":
        search_type = SearchType.AUTHOR
    elif step.kind == "isbn":
        search_type = SearchType.ISBN

    return MetadataSearchOptions(
        query=step.query,
        search_type=search_type,
        limit=limit,
        fields=fields,
        language=language,
    )


def _resolve_active_providers() -> tuple[list[MetadataProvider], list[ProviderStatus]]:
    """Instantiate every enabled, configured provider; report status for the rest."""
    active: list[MetadataProvider] = []
    statuses: dict[str, ProviderStatus] = {}

    for entry in list_providers():
        name = entry["name"]
        display_name = entry["display_name"]

        if not is_provider_enabled(name):
            statuses[name] = ProviderStatus(
                name=name, display_name=display_name, status=_STATUS_DISABLED
            )
            continue

        try:
            provider = get_provider(name, **get_provider_kwargs(name))
        except (TypeError, ValueError) as exc:
            logger.warning("Could not initialize metadata provider %s: %s", name, exc)
            statuses[name] = ProviderStatus(
                name=name,
                display_name=display_name,
                status=_STATUS_ERROR,
                message="Could not initialize provider",
            )
            continue

        if not provider.is_available():
            statuses[name] = ProviderStatus(
                name=name,
                display_name=display_name,
                status=_STATUS_NOT_CONFIGURED,
                message="Enabled but not fully configured (e.g. missing API key)",
            )
            continue

        active.append(provider)
        statuses[name] = ProviderStatus(
            name=name, display_name=display_name, status=_STATUS_NO_RESULTS
        )

    return active, list(statuses.values())


def _run_one_provider(
    provider: MetadataProvider, options: MetadataSearchOptions
) -> list[BookMetadata]:
    started = time.monotonic()
    try:
        books = provider.search(options)
    except Exception:
        logger.exception("Metadata provider %s search failed", provider.name)
        raise
    else:
        logger.debug(
            "%s search '%s' returned %d result(s) in %.0fms",
            provider.display_name,
            options.query,
            len(books),
            (time.monotonic() - started) * 1000,
        )
        return books


def discover_books(
    query_steps: list[QueryStep],
    *,
    limit_per_provider: int = DEFAULT_LIMIT_PER_PROVIDER,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    min_results_to_stop: int = DEFAULT_MIN_RESULTS_TO_STOP,
    max_workers: int = DEFAULT_MAX_WORKERS,
    language: str | None = None,
) -> DiscoveryResult:
    """Fan out `query_steps` across every enabled, configured metadata provider.

    Steps are tried in order (see `shelfmark.core.query_normalization.
    build_progressive_queries`); each step queries every active provider in parallel.
    Iteration stops once enough raw results have accumulated, or once the steps are
    exhausted. A provider that raises or times out never prevents another provider - or
    another step - from being tried; its status is recorded instead.
    """
    active_providers, status_list = _resolve_active_providers()
    statuses_by_name = {status.name: status for status in status_list}

    books: list[BookMetadata] = []

    if not active_providers or not query_steps:
        return DiscoveryResult(books=books, provider_statuses=status_list)

    worker_count = max(1, min(max_workers, len(active_providers)))

    for step in query_steps:
        if len(books) >= min_results_to_stop:
            break

        options = _build_search_options(step, limit=limit_per_provider, language=language)
        step_started = time.monotonic()

        # Not a `with` block on purpose: a `with ThreadPoolExecutor(...)` blocks on exit
        # until every submitted call returns, which would defeat `timeout_seconds` for a
        # provider whose own HTTP call is still in flight. `cancel_futures=True` drops
        # anything not yet started; a call already running keeps running in its thread
        # but its result is simply never awaited - reported as a timeout instead.
        executor = ThreadPoolExecutor(max_workers=worker_count)
        try:
            future_to_provider = {
                executor.submit(_run_one_provider, provider, options): provider
                for provider in active_providers
            }
            done, not_done = wait(future_to_provider, timeout=timeout_seconds)

            elapsed_ms = int((time.monotonic() - step_started) * 1000)
            for future in done:
                provider = future_to_provider[future]
                status = statuses_by_name[provider.name]
                try:
                    results = future.result()
                except Exception as exc:  # noqa: BLE001 - isolated per provider, reported not raised
                    if status.status != _STATUS_OK:
                        status.status = _STATUS_ERROR
                        status.message = str(exc) or exc.__class__.__name__
                    continue
                status.elapsed_ms += elapsed_ms
                if results:
                    status.status = _STATUS_OK
                    status.count += len(results)
                    status.message = None
                    books.extend(results)

            for future in not_done:
                provider = future_to_provider[future]
                status = statuses_by_name[provider.name]
                if status.status != _STATUS_OK:
                    status.status = _STATUS_ERROR
                    status.message = f"Timed out after {timeout_seconds:.0f}s"
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    return DiscoveryResult(books=books, provider_statuses=list(statuses_by_name.values()))
