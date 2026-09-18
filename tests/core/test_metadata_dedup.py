"""Tests for cross-provider result merging/deduplication."""

from shelfmark.core.metadata_dedup import merge_results
from shelfmark.metadata_providers import BookMetadata


def _book(provider, provider_id, title, **kwargs):
    return BookMetadata(provider=provider, provider_id=provider_id, title=title, **kwargs)


class TestExactMatching:
    def test_isbn13_merges_across_providers(self):
        a = _book("openlibrary", "OL1W", "Dune", isbn_13="9780441013593")
        b = _book("googlebooks", "vol-1", "Dune (Google)", isbn_13="9780441013593")

        merged = merge_results([a, b])

        assert len(merged) == 1
        assert merged[0].match_basis == "isbn13"
        assert {ref.provider for ref in merged[0].sources} == {"openlibrary", "googlebooks"}

    def test_isbn10_merges_across_providers(self):
        a = _book("openlibrary", "OL1W", "Dune", isbn_10="0441013597")
        b = _book("googlebooks", "vol-1", "Dune (Google)", isbn_10="0441013597")

        merged = merge_results([a, b])

        assert len(merged) == 1
        assert merged[0].match_basis == "isbn10"

    def test_isbn_with_hyphens_still_matches(self):
        a = _book("openlibrary", "OL1W", "Dune", isbn_13="978-0-441-01359-3")
        b = _book("googlebooks", "vol-1", "Dune", isbn_13="9780441013593")

        merged = merge_results([a, b])

        assert len(merged) == 1

    def test_md5_merges_across_providers(self):
        a = _book("annas_archive", "abc123", "Dune", md5="d41d8cd98f00b204e9800998ecf8427e")
        b = _book("openlibrary", "OL1W", "Dune", md5="D41D8CD98F00B204E9800998ECF8427E")

        merged = merge_results([a, b])

        assert len(merged) == 1
        assert merged[0].match_basis == "md5"

    def test_libgen_and_annas_archive_merge_on_identical_md5(self):
        # The Direct-mode discovery scenario: Libgen found the file independently of
        # Anna's Archive, but both happen to report the same underlying file.
        a = _book(
            "libgen_search",
            "d41d8cd98f00b204e9800998ecf8427e",
            "Meurtre au festival des citrouilles",
            authors=["Ava Manceau"],
            md5="d41d8cd98f00b204e9800998ecf8427e",
        )
        b = _book(
            "annas_archive",
            "abc123",
            "Meurtre au festival des citrouilles",
            md5="d41d8cd98f00b204e9800998ecf8427e",
        )

        merged = merge_results([a, b])

        assert len(merged) == 1
        assert merged[0].match_basis == "md5"
        assert {ref.provider for ref in merged[0].sources} == {"libgen_search", "annas_archive"}

    def test_same_provider_and_id_merges(self):
        # The same record reached via two different progressive query steps.
        a = _book("openlibrary", "OL1W", "Dune")
        b = _book("openlibrary", "OL1W", "Dune")

        merged = merge_results([a, b])

        assert len(merged) == 1
        assert merged[0].match_basis == "provider_id"

    def test_title_author_language_merges_without_isbn(self):
        a = _book(
            "openlibrary",
            "OL1W",
            "Meurtre au festival des citrouilles",
            authors=["Ava Manceau"],
            language="fr",
        )
        b = _book(
            "googlebooks",
            "vol-2",
            "Meurtre au festival des citrouilles",
            authors=["Ava Manceau"],
            language="fr",
        )

        merged = merge_results([a, b])

        assert len(merged) == 1
        assert merged[0].match_basis == "title_author_lang"

    def test_title_author_language_does_not_merge_different_languages(self):
        a = _book("openlibrary", "OL1W", "Dune", authors=["Frank Herbert"], language="en")
        b = _book("googlebooks", "vol-2", "Dune", authors=["Frank Herbert"], language="fr")

        merged = merge_results([a, b])

        # Different-language editions may still coincide via fuzzy matching (same
        # title, same author), but not via the exact title_author_lang key.
        assert len(merged) == 1
        assert merged[0].match_basis == "fuzzy"


class TestFuzzyMatching:
    def test_close_title_and_matching_author_merges(self):
        # "Meurtre"/"Meurtres" differ by one letter - close but not identical once
        # normalized, so this only merges via the fuzzy tier, not title_author_lang.
        a = _book(
            "openlibrary",
            "OL1W",
            "Meurtre au festival des citrouilles",
            authors=["Ava Manceau"],
        )
        b = _book(
            "googlebooks",
            "vol-2",
            "Meurtres au festival des citrouilles",
            authors=["Ava Manceau"],
        )

        merged = merge_results([a, b])

        assert len(merged) == 1
        assert merged[0].match_basis == "fuzzy"

    def test_similar_title_but_different_author_does_not_merge(self):
        a = _book("openlibrary", "OL1W", "The Great Escape", authors=["Alice Author"])
        b = _book("googlebooks", "vol-2", "The Great Escape", authors=["Bob Writer"])

        merged = merge_results([a, b])

        assert len(merged) == 2

    def test_dissimilar_titles_do_not_merge(self):
        a = _book("openlibrary", "OL1W", "Dune", authors=["Frank Herbert"])
        b = _book("googlebooks", "vol-2", "Foundation", authors=["Frank Herbert"])

        merged = merge_results([a, b])

        assert len(merged) == 2

    def test_missing_author_on_either_side_prevents_fuzzy_merge(self):
        # Prudence: a title-only match with no author corroboration on one side is
        # left as two separate books rather than guessed into one.
        a = _book("openlibrary", "OL1W", "Meurtre au festival des citrouilles")
        b = _book(
            "googlebooks",
            "vol-2",
            "Meurtre au festival des citrouilles",
            authors=["Ava Manceau"],
        )

        merged = merge_results([a, b])

        assert len(merged) == 2

    def test_libgen_result_does_not_merge_abusively_on_shared_author_name(self):
        # Two different books that merely share the author "Ava Manceau" must not be
        # collapsed into one entry just because a title-ish substring overlaps.
        a = _book(
            "libgen_search",
            "md5-a",
            "Meurtre au festival des citrouilles",
            authors=["Ava Manceau"],
            md5="md5-a",
        )
        b = _book(
            "openlibrary",
            "OL2W",
            "Crime au marche de Noel",
            authors=["Ava Manceau"],
        )

        merged = merge_results([a, b])

        assert len(merged) == 2

    def test_libgen_result_with_close_title_and_author_merges_via_fuzzy(self):
        a = _book(
            "libgen_search",
            "md5-a",
            "Meurtre au festival des citrouilles",
            authors=["Ava Manceau"],
            md5="md5-a",
        )
        b = _book(
            "openlibrary",
            "OL1W",
            "Meurtres au festival des citrouilles",  # one-letter difference
            authors=["Ava Manceau"],
        )

        merged = merge_results([a, b])

        assert len(merged) == 1
        assert merged[0].match_basis == "fuzzy"

    def test_custom_thresholds_are_respected(self):
        a = _book("openlibrary", "OL1W", "The Hobbit", authors=["J.R.R. Tolkien"])
        b = _book("googlebooks", "vol-2", "The Hobbitt", authors=["J.R.R. Tolkien"])

        # The default threshold merges this near-identical pair (ratio ~0.95); a
        # stricter threshold should keep them separate instead.
        default_merge = merge_results([a, b])
        assert len(default_merge) == 1

        strict_merge = merge_results([a, b], fuzzy_title_threshold=0.99)
        assert len(strict_merge) == 2


class TestNoMatch:
    def test_unrelated_books_stay_separate(self):
        a = _book("openlibrary", "OL1W", "Dune", authors=["Frank Herbert"])
        b = _book("googlebooks", "vol-2", "The Hobbit", authors=["J.R.R. Tolkien"])

        merged = merge_results([a, b])

        assert len(merged) == 2
        assert merged[0].match_basis == "single"
        assert merged[1].match_basis == "single"

    def test_empty_input(self):
        assert merge_results([]) == []


class TestMergedBookShape:
    def test_richer_record_becomes_primary(self):
        sparse = _book("openlibrary", "OL1W", "Dune", isbn_13="9780441013593")
        rich = _book(
            "googlebooks",
            "vol-1",
            "Dune",
            isbn_13="9780441013593",
            authors=["Frank Herbert"],
            description="A desert planet epic.",
            cover_url="https://example.com/cover.jpg",
            publisher="Ace Books",
            publish_year=1965,
            language="en",
        )

        merged = merge_results([sparse, rich])

        assert merged[0].book.provider == "googlebooks"
        assert merged[0].book.description == "A desert planet epic."

    def test_three_providers_merge_into_one_entry_with_all_sources(self):
        a = _book("openlibrary", "OL1W", "Dune", isbn_13="9780441013593")
        b = _book("googlebooks", "vol-1", "Dune", isbn_13="9780441013593")
        c = _book("hardcover", "hc-1", "Dune", isbn_13="9780441013593")

        merged = merge_results([a, b, c])

        assert len(merged) == 1
        assert len(merged[0].sources) == 3
        assert {ref.provider for ref in merged[0].sources} == {
            "openlibrary",
            "googlebooks",
            "hardcover",
        }

    def test_relevance_score_higher_with_more_corroborating_sources(self):
        a = _book("openlibrary", "OL1W", "Dune", isbn_13="9780441013593")
        b = _book("googlebooks", "vol-1", "Dune", isbn_13="9780441013593")
        c = _book("hardcover", "hc-1", "Dune", isbn_13="9780441013593")
        single = _book("moly", "m-1", "Some Other Book")

        merged = merge_results([a, b, c, single])
        by_title = {m.book.title: m for m in merged}

        assert by_title["Dune"].relevance_score > by_title["Some Other Book"].relevance_score

    def test_result_order_is_stable_by_first_occurrence(self):
        first = _book("openlibrary", "OL1W", "Alpha")
        second = _book("googlebooks", "vol-2", "Beta")
        third = _book("hardcover", "hc-3", "Gamma")

        merged = merge_results([first, second, third])

        assert [m.book.title for m in merged] == ["Alpha", "Beta", "Gamma"]
