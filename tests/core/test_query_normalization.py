"""Tests for cross-provider query normalization and the progressive search cascade."""

import pytest

from shelfmark.core.query_normalization import (
    QueryStep,
    build_progressive_queries,
    extract_series_index,
    normalize_for_matching,
    split_subtitle,
)


class TestNormalizeForMatching:
    def test_casefolds_and_strips_accents(self):
        assert normalize_for_matching("Ava Manceau") == normalize_for_matching("AVA MANCEAU")
        assert normalize_for_matching("Éxécuté") == normalize_for_matching("execute")

    def test_strips_punctuation_and_collapses_whitespace(self):
        assert normalize_for_matching("Meurtre, au festival !!") == normalize_for_matching(
            "meurtre   au    festival"
        )

    def test_empty_input(self):
        assert normalize_for_matching("") == ""
        assert normalize_for_matching(None) == ""

    def test_display_text_is_never_mutated(self):
        # Sanity check that this module never exposes a "normalized" value where a
        # display value was expected - callers must always keep the raw string
        # alongside the normalized one (see QueryStep / split_subtitle / etc).
        raw = "Ça Va, Été ?"
        assert normalize_for_matching(raw) != raw


class TestSplitSubtitle:
    def test_splits_on_colon(self):
        assert split_subtitle("Dune: The Sequel") == ("Dune", "The Sequel")

    def test_splits_on_padded_colon(self):
        assert split_subtitle("Dune : The Sequel") == ("Dune", "The Sequel")

    def test_no_subtitle_returns_original(self):
        assert split_subtitle("Meurtre au festival des citrouilles") == (
            "Meurtre au festival des citrouilles",
            None,
        )

    def test_trailing_colon_without_subtitle_is_not_split(self):
        assert split_subtitle("Cliffhanger:") == ("Cliffhanger:", None)

    def test_empty_input(self):
        assert split_subtitle("") == ("", None)
        assert split_subtitle(None) == ("", None)


class TestExtractSeriesIndex:
    @pytest.mark.parametrize(
        ("title", "expected_title", "expected_index"),
        [
            ("Le Nom du Vent Tome 1", "Le Nom du Vent", 1.0),
            ("Le Nom du Vent, Tome 2", "Le Nom du Vent", 2.0),
            ("Foundation Book 3", "Foundation", 3.0),
            ("Foundation, Book 3", "Foundation", 3.0),
            ("Mistborn Vol. 4", "Mistborn", 4.0),
            ("Mistborn Volume 4", "Mistborn", 4.0),
            ("Discworld #5", "Discworld", 5.0),
            ("Harry Potter t. 1", "Harry Potter", 1.0),
            ("Harry Potter T.1", "Harry Potter", 1.0),
        ],
    )
    def test_extracts_index_and_strips_marker(self, title, expected_title, expected_index):
        assert extract_series_index(title) == (expected_title, expected_index)

    def test_no_marker_returns_title_unchanged(self):
        assert extract_series_index("Meurtre au festival des citrouilles") == (
            "Meurtre au festival des citrouilles",
            None,
        )

    def test_does_not_eat_words_that_merely_contain_a_marker(self):
        # "Tomek" and "Booker" must not be mistaken for "Tome"/"Book" markers.
        title, index = extract_series_index("Tomek and the Booker Prize")
        assert index is None
        assert title == "Tomek and the Booker Prize"

    def test_empty_input(self):
        assert extract_series_index("") == ("", None)
        assert extract_series_index(None) == ("", None)


class TestBuildProgressiveQueries:
    def test_title_and_author_produces_full_cascade(self):
        steps = build_progressive_queries(
            "Ava Manceau", title="Meurtre au festival des citrouilles", author="Ava Manceau"
        )
        kinds = [step.kind for step in steps]
        assert kinds == ["title_author", "title", "author"]
        assert steps[0].query == "Meurtre au festival des citrouilles Ava Manceau"
        assert steps[1].query == "Meurtre au festival des citrouilles"
        assert steps[2].query == "Ava Manceau"

    def test_strips_series_marker_from_title_steps(self):
        steps = build_progressive_queries(
            "", title="Le Nom du Vent Tome 1", author="Patrick Rothfuss"
        )
        title_step = next(step for step in steps if step.kind == "title")
        assert title_step.query == "Le Nom du Vent"

    def test_series_and_isbn_steps_included_when_provided(self):
        steps = build_progressive_queries(
            "", title="Dune", author="Frank Herbert", series="Dune Saga", isbn="9780441013593"
        )
        kinds = [step.kind for step in steps]
        assert kinds == ["title_author", "title", "author", "series", "isbn"]
        assert steps[-1].query == "9780441013593"

    def test_duplicate_steps_are_dropped(self):
        # Title alone would normalize to the same text as title+author when there is
        # no author, so only one step should be produced.
        steps = build_progressive_queries("", title="Dune")
        assert [step.kind for step in steps] == ["title"]

    def test_raw_query_used_as_title_fallback(self):
        steps = build_progressive_queries("Meurtre au festival des citrouilles")
        assert len(steps) == 1
        assert steps[0].kind == "title"
        assert steps[0].query == "Meurtre au festival des citrouilles"

    def test_subtitle_is_split_off_raw_query_before_searching(self):
        steps = build_progressive_queries("Dune: The Sequel")
        assert steps[0].query == "Dune"

    def test_empty_input_produces_no_steps(self):
        assert build_progressive_queries("") == []

    def test_query_step_is_frozen(self):
        step = QueryStep(kind="title", query="Dune", normalized="dune")
        with pytest.raises(AttributeError):
            step.query = "Other"  # type: ignore[misc]
