"""Query normalization and progressive search-variant building for cross-provider discovery.

Shared by the metadata-provider orchestrator (Direct mode fallback, see
`shelfmark.core.metadata_orchestrator`) so every provider is compared on the same terms
regardless of accents, casing, "Tome 1"-style series markers, or a title/subtitle split -
and so `shelfmark.core.metadata_dedup` merges results that are really the same book
instead of missing them over a diacritic or a stray "T.3".

Normalization here is only ever used for *matching*. The raw text a provider returned, or
the raw text the user typed, is what reaches the UI - nothing in this module lowercases or
strips accents from anything that gets displayed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_PUNCTUATION_PATTERN = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_PATTERN = re.compile(r"\s+")

# Trailing/leading series-index markers: "Tome 1", "T.1", "t. 1", "Book 1", "Vol 1",
# "Volume 1", "#1". Matched with word boundaries so it doesn't eat "Tomek" or "Booker".
_SERIES_INDEX_PATTERN = re.compile(
    r"""
    (?:^|[\s,(\-])
    (?:
        tome\s*(?P<tome>\d+(?:[.,]\d+)?)
        |t\.?\s*(?P<t>\d+(?:[.,]\d+)?)
        |book\s*(?P<book>\d+(?:[.,]\d+)?)
        |vol(?:ume)?\.?\s*(?P<vol>\d+(?:[.,]\d+)?)
        |\#(?P<hash>\d+(?:[.,]\d+)?)
    )
    (?:[\s,)\-]|$)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Separators that introduce a subtitle. Order matters: the padded form is tried first so
# "Foo: Bar" (no leading space) still matches via the second entry.
_SUBTITLE_SEPARATORS = (" : ", ": ")


def normalize_for_matching(text: str | None) -> str:
    """Casefold, strip accents (Unicode NFKD) and punctuation, collapse whitespace.

    Same technique already used for language aliases (`shelfmark.core.languages._fold`)
    and Anna's Archive title comparison - kept as a small standalone helper here since
    those two normalize for different purposes and shouldn't share a dependency.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    no_punctuation = _PUNCTUATION_PATTERN.sub(" ", stripped)
    return _WHITESPACE_PATTERN.sub(" ", no_punctuation).strip().casefold()


def split_subtitle(title: str | None) -> tuple[str, str | None]:
    """Split "Main Title: Subtitle" into (main, subtitle). Preserves original casing.

    Returns the unsplit, stripped title and None when there is no ": " separator, or
    when splitting on it would leave either side empty (e.g. a title that merely ends
    with a colon).
    """
    if not title:
        return "", None
    for separator in _SUBTITLE_SEPARATORS:
        if separator in title:
            main, _, subtitle = title.partition(separator)
            main = main.strip()
            subtitle = subtitle.strip()
            if main and subtitle:
                return main, subtitle
    return title.strip(), None


def extract_series_index(title: str | None) -> tuple[str, float | None]:
    """Pull a series index ("Tome 1", "T.2", "Book 3", "Vol. 4", "#5") out of a title.

    Returns the title with the marker removed (whitespace collapsed) and the index as
    a float, or the original stripped title and None when no marker is found.
    """
    if not title:
        return "", None
    match = _SERIES_INDEX_PATTERN.search(title)
    if not match:
        return title.strip(), None
    raw_value = next(value for value in match.groupdict().values() if value is not None)
    try:
        index = float(raw_value.replace(",", "."))
    except ValueError:
        return title.strip(), None
    cleaned = title[: match.start()] + " " + title[match.end() :]
    cleaned = _WHITESPACE_PATTERN.sub(" ", cleaned).strip(" -,")
    return cleaned or title.strip(), index


@dataclass(frozen=True)
class QueryStep:
    """One step of the progressive discovery search cascade.

    `query`/`title`/`author` are the raw text a provider should search with (and stay
    fit for display). `normalized` exists only to detect a step that would repeat an
    earlier one after normalization, so a query with no distinct author does not run
    the same search twice.
    """

    kind: str  # "title_author" | "title" | "author" | "series" | "isbn"
    query: str
    normalized: str
    title: str | None = None
    author: str | None = None


def build_progressive_queries(
    raw_query: str,
    *,
    title: str | None = None,
    author: str | None = None,
    series: str | None = None,
    isbn: str | None = None,
) -> list[QueryStep]:
    """Build the a-to-e progressive search cascade: title+author, title, author, series, ISBN.

    `raw_query` is used to derive a title (after splitting off any subtitle) when the
    caller has not already resolved one. A series-index marker such as "Tome 1" is
    stripped from whichever title text feeds the title/title+author steps, so "Le Nom
    du Vent Tome 1" and "Le Nom du Vent" match the same provider results.

    A step is skipped when it has nothing to search, or when it would normalize to the
    same text as an earlier step (so "title" is dropped when "title_author" already
    covers it and there is no author). Callers exhaust the returned list in order,
    stopping once enough combined results have come back.
    """
    resolved_title = title
    if resolved_title is None and raw_query:
        resolved_title, _ = split_subtitle(raw_query)

    cleaned_title, _series_index = (
        extract_series_index(resolved_title)
        if resolved_title
        else (
            None,
            None,
        )
    )
    cleaned_title = cleaned_title or resolved_title

    steps: list[QueryStep] = []
    seen: set[str] = set()

    def _add(
        kind: str,
        query_value: str | None,
        *,
        step_title: str | None = None,
        step_author: str | None = None,
    ) -> None:
        if not query_value:
            return
        query_value = query_value.strip()
        if not query_value:
            return
        normalized = normalize_for_matching(query_value)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        steps.append(
            QueryStep(
                kind=kind,
                query=query_value,
                normalized=normalized,
                title=step_title,
                author=step_author,
            )
        )

    if cleaned_title and author:
        _add(
            "title_author",
            f"{cleaned_title} {author}",
            step_title=cleaned_title,
            step_author=author,
        )
    if cleaned_title:
        _add("title", cleaned_title, step_title=cleaned_title)
    if author:
        _add("author", author, step_author=author)
    if series:
        _add("series", series)
    if isbn:
        _add("isbn", isbn)

    if not steps and raw_query:
        _add("title", raw_query, step_title=raw_query)

    return steps
