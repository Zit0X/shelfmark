# Direct Mode Metadata Fallback

In **Direct** search mode, Shelfmark normally searches only the Direct Download source
(Anna's Archive, plus any other release provider registered under Direct Download). If that
source has no results for a query - because it's down, blocked, or simply doesn't have the
book - the search returns nothing, even when the book is easy to find elsewhere.

This feature lets Direct mode also search enabled **metadata providers** (Open Library,
Google Books, Hardcover, Moly) and **Libgen's own catalogue** for discovery purposes, and
merges the results with whatever Anna's Archive found. A book found only by one of these
shows up as a discovery card with no download of its own yet; opening it searches your
enabled release sources (Prowlarr, Newznab, IRC, LibGen, Direct Download, ...) the same way
Universal mode already does. Downloads always go through that existing Source Priority
logic - this feature only changes what shows up in Direct mode's search results, not how
anything gets downloaded.

Unlike the bibliographic providers, a Libgen discovery result already carries the md5 of a
real file (Libgen's catalogue is a file index, not just a bibliography) - metadata_dedup
uses it to merge with an Anna's Archive result for the same file when both are present. That
md5 is never required, though: a Libgen-only result displays and opens normally without one,
which is what makes this useful when Anna's Archive is down, blocked, or simply doesn't
index a given book (self-published and small-press titles, some comics/manga) - see the
"Ava Manceau" case this feature was built against, in
`tests/core/test_direct_search_route.py`.

Z-Library is deliberately not part of this fan-out: it has no publicly documented,
ToS-compatible search API, and this project does not implement anti-bot bypass to work
around that. `shelfmark/metadata_providers/zlibrary.py` registers a stub provider that is
permanently unavailable, purely so the interface has a documented placeholder instead of
silent absence. This is unrelated to the separate "Z-Library mirror" setting, which only
resolves *downloads* for a file Anna's Archive already found.

It's off by default and changes nothing until you turn it on.

## Enabling it

In **Settings → Search Mode** (only visible while Search Mode is set to Direct):

1. Turn on **Enable Metadata Provider Fallback** (`DIRECT_MODE_METADATA_FALLBACK_ENABLED`).
2. Enable and configure at least one participant:
   - A metadata provider under **Settings → Metadata Providers** (Open Library needs no
     API key; Google Books and Hardcover need one).
   - Libgen under **Settings → Libgen Search**: turn on **Enable Libgen Search**
     (`LIBGEN_SEARCH_ENABLED`) and configure at least one Libgen mirror under
     **Settings → Mirrors**. This is the *same* toggle Universal mode already uses for
     Libgen - there is no separate "enable for Direct mode" switch. A user who has decided
     Libgen search is acceptable for their deployment has decided it for both modes.

   Only participants you enable this way take part - the fallback switch above doesn't
   enable any of them by itself, and none is queried until both it and the participant's
   own toggle are on. For Libgen specifically, all three of these must be true before it
   participates in Direct-mode discovery: `DIRECT_MODE_METADATA_FALLBACK_ENABLED=true`,
   `LIBGEN_SEARCH_ENABLED=true`, and at least one Libgen mirror configured.

## Fallback strategy

**Fallback Strategy** controls when metadata providers are queried relative to Anna's
Archive:

- **Only when Anna's Archive fails or finds nothing** (default) - the conservative choice:
  metadata providers are only queried when the Direct Download search comes back empty or
  errors out, so a successful Anna's Archive search never triggers extra API calls.
- **Always, in parallel with Anna's Archive** - queries every enabled metadata provider on
  every search, alongside Anna's Archive, so results that only a metadata provider has are
  never delayed behind an Anna's Archive round-trip. This makes more API calls per search.

## Matching and merging

A book reported by more than one source is shown once, not once per source. Results are
matched in this order: exact ISBN-13, exact ISBN-10, exact MD5 (when a source happens to
report one), the same provider reporting the same record twice, then normalized title +
author + language, and finally a conservative fuzzy title/author match as a last resort - two
results are only merged this way when both their titles and authors are close enough; a title
match alone is never enough. See `shelfmark/core/metadata_dedup.py` for the exact thresholds.

Each result also carries a status list naming every provider that was searched and what
happened - "Open Library: 12 results", "Google Books: unavailable (timed out)", "Hardcover:
disabled" - so a quiet result never reads as "nothing exists," only as "here's what each
source did."

## Other settings

- **Metadata Provider Timeout** - how long to wait on each metadata provider before treating
  it as unavailable for that search (default 8s).
- **Max Results per Metadata Provider** - how many results to request from each provider per
  search (default 20).

See [Environment Variables](environment-variables.md#search-mode) for the corresponding
`DIRECT_MODE_*` environment variables.
