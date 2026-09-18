"""Merge and deduplicate `BookMetadata` results gathered from several search providers.

Used by `shelfmark.core.metadata_orchestrator` to turn what Open Library, Google Books,
Hardcover (and, when it participates, Anna's Archive) each independently reported into one
list of books, each annotated with every provider that found it.

Matching is a cascade of increasingly loose keys, in priority order:

1. ISBN-13 exact
2. ISBN-10 exact
3. MD5 exact (only set when a provider happens to know one - see `BookMetadata.md5`)
4. Same provider + same provider_id (the same record reached the cascade twice, e.g. via
   two different progressive search steps)
5. Normalized title + normalized author + language, all exact
6. Fuzzy title/author match, prudently thresholded

Two books are merged only when they share at least one key from that cascade - fuzzy
matching is the last resort, not the first check, and a pair below the fuzzy threshold is
left as two separate results rather than guessed into one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from shelfmark.core.languages import normalize_language
from shelfmark.core.query_normalization import normalize_for_matching
from shelfmark.core.search_plan import pick_search_author

if TYPE_CHECKING:
    from shelfmark.metadata_providers import BookMetadata

# Below this, two titles are treated as different books even if their authors match.
DEFAULT_FUZZY_TITLE_THRESHOLD = 0.92
# Below this, two authors are treated as different people even if their titles match.
DEFAULT_FUZZY_AUTHOR_THRESHOLD = 0.6

# Match-basis labels, most specific first - used to report the strongest reason a
# cluster of more than one record was merged, even when some pairs within it only
# share a looser key.
_BASIS_PRIORITY = ("isbn13", "isbn10", "md5", "provider_id", "title_author_lang", "fuzzy")

_ISBN_CLEAN_PATTERN = re.compile(r"[^0-9Xx]")


def _normalize_isbn(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _ISBN_CLEAN_PATTERN.sub("", value).upper()
    return cleaned or None


def _normalize_md5(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lower()
    return cleaned or None


@dataclass(frozen=True)
class ProviderRef:
    """One provider's record for a merged book."""

    provider: str
    provider_id: str
    provider_display_name: str | None = None
    source_url: str | None = None


@dataclass
class MergedBook:
    """A book, possibly reported by more than one provider, collapsed into one entry."""

    book: BookMetadata
    sources: list[ProviderRef] = field(default_factory=list)
    # Why this cluster was merged: "isbn13" | "isbn10" | "md5" | "provider_id" |
    # "title_author_lang" | "fuzzy" | "single" (only one provider reported it).
    match_basis: str = "single"
    relevance_score: float = 1.0


def _provider_ref(book: BookMetadata) -> ProviderRef:
    return ProviderRef(
        provider=book.provider,
        provider_id=book.provider_id,
        provider_display_name=book.provider_display_name,
        source_url=book.source_url,
    )


def _exact_keys(book: BookMetadata) -> list[tuple[object, ...]]:
    """Every exact-match key this book qualifies for, most specific first."""
    keys: list[tuple[object, ...]] = []

    isbn_13 = _normalize_isbn(book.isbn_13)
    if isbn_13:
        keys.append(("isbn13", isbn_13))

    isbn_10 = _normalize_isbn(book.isbn_10)
    if isbn_10:
        keys.append(("isbn10", isbn_10))

    md5 = _normalize_md5(book.md5)
    if md5:
        keys.append(("md5", md5))

    keys.append(("provider_id", book.provider, book.provider_id))

    normalized_title = normalize_for_matching(book.search_title or book.title)
    normalized_author = normalize_for_matching(pick_search_author(book))
    if normalized_title and normalized_author:
        language = normalize_language(book.language) or ""
        keys.append(("title_author_lang", normalized_title, normalized_author, language))

    return keys


class _UnionFind:
    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, index: int) -> int:
        while self._parent[index] != index:
            self._parent[index] = self._parent[self._parent[index]]
            index = self._parent[index]
        return index

    def union(self, a: int, b: int) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_b] = root_a


def _fuzzy_match(
    a: BookMetadata,
    b: BookMetadata,
    *,
    title_threshold: float,
    author_threshold: float,
) -> bool:
    """Whether two books are plausibly the same work by title/author similarity alone."""
    title_a = normalize_for_matching(a.search_title or a.title)
    title_b = normalize_for_matching(b.search_title or b.title)
    if not title_a or not title_b:
        return False

    title_ratio = SequenceMatcher(None, title_a, title_b).ratio()
    if title_ratio < title_threshold:
        return False

    author_a = normalize_for_matching(pick_search_author(a))
    author_b = normalize_for_matching(pick_search_author(b))
    if not author_a or not author_b:
        # Neither side can vouch for the author - the title match alone isn't enough to
        # merge prudently, so this pair is left separate rather than guessed into one.
        return False

    author_ratio = SequenceMatcher(None, author_a, author_b).ratio()
    return author_ratio >= author_threshold


def _richness(book: BookMetadata) -> int:
    """How much optional metadata a book carries, used to pick the primary record."""
    optional_values = (
        book.authors,
        book.isbn_10,
        book.isbn_13,
        book.cover_url,
        book.description,
        book.publisher,
        book.publish_year,
        book.language,
        book.source_url,
        book.subtitle,
        book.series_name,
    )
    return sum(1 for value in optional_values if value)


def merge_results(
    candidates: list[BookMetadata],
    *,
    fuzzy_title_threshold: float = DEFAULT_FUZZY_TITLE_THRESHOLD,
    fuzzy_author_threshold: float = DEFAULT_FUZZY_AUTHOR_THRESHOLD,
) -> list[MergedBook]:
    """Merge `BookMetadata` results from one or more providers into deduplicated books.

    Order of `candidates` is preserved for the primary record chosen within a cluster
    when richness is tied, so callers that care about provider priority should list
    higher-priority providers' results first.
    """
    if not candidates:
        return []

    union_find = _UnionFind(len(candidates))
    key_to_first_index: dict[tuple[object, ...], int] = {}
    basis_by_pair: dict[tuple[int, int], str] = {}

    for index, book in enumerate(candidates):
        for key in _exact_keys(book):
            existing_index = key_to_first_index.get(key)
            if existing_index is None:
                key_to_first_index[key] = index
                continue
            if union_find.find(existing_index) != union_find.find(index):
                union_find.union(existing_index, index)
            pair = (min(existing_index, index), max(existing_index, index))
            basis_by_pair.setdefault(pair, str(key[0]))

    # Fuzzy pass: only between clusters that have no exact-match link yet, and only when
    # a query returned few enough candidates that the extra comparisons stay cheap - a
    # single search's combined result set from a handful of providers, never a bulk job.
    cluster_representatives: dict[int, int] = {}
    for index in range(len(candidates)):
        root = union_find.find(index)
        cluster_representatives.setdefault(root, index)

    representative_indices = list(cluster_representatives.values())
    for i, index_a in enumerate(representative_indices):
        for index_b in representative_indices[i + 1 :]:
            if union_find.find(index_a) == union_find.find(index_b):
                continue
            if _fuzzy_match(
                candidates[index_a],
                candidates[index_b],
                title_threshold=fuzzy_title_threshold,
                author_threshold=fuzzy_author_threshold,
            ):
                union_find.union(index_a, index_b)
                pair = (min(index_a, index_b), max(index_a, index_b))
                basis_by_pair[pair] = "fuzzy"

    clusters: dict[int, list[int]] = {}
    for index in range(len(candidates)):
        clusters.setdefault(union_find.find(index), []).append(index)

    merged: list[MergedBook] = []
    for root in sorted(clusters, key=lambda r: min(clusters[r])):
        member_indices = clusters[root]
        member_indices.sort()
        members = [candidates[i] for i in member_indices]

        primary_index = max(
            member_indices,
            key=lambda i: (_richness(candidates[i]), -i),
        )
        primary = candidates[primary_index]

        if len(members) == 1:
            match_basis = "single"
        else:
            member_set = set(member_indices)
            bases_in_cluster = {
                basis
                for pair, basis in basis_by_pair.items()
                if pair[0] in member_set and pair[1] in member_set
            }
            match_basis = next(
                (basis for basis in _BASIS_PRIORITY if basis in bases_in_cluster),
                "fuzzy",
            )

        relevance_score = min(1.0, 0.7 + 0.15 * (len(members) - 1)) if len(members) > 1 else 0.7

        merged.append(
            MergedBook(
                book=primary,
                sources=[_provider_ref(member) for member in members],
                match_basis=match_basis,
                relevance_score=relevance_score,
            )
        )

    return merged
