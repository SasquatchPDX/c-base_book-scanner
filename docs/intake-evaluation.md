# Offline intake decision scorecard

Issue #165 measures the actual COPY-source intake pipeline, rather than a second
implementation of its scoring rules. The committed [corpus](../tests/fixtures/intake_evaluation/corpus.json)
has **31 hand-labelled scenarios / 33 files**. It generates real EPUB containers,
TXT headers and Calibre-style OPF sidecars, then runs metadata extraction,
normalization, candidate ranking, author/edition guards, sibling-format handling,
local suggestions, archiving and persistent review-task creation.

Only the provider boundary is replaced with frozen records or explicit outages.
These are synthetic scenarios: titles/authors are fictional and the checksummed
ISBNs are protocol fixtures, not asserted bibliographic assignments. This is a
regression and threshold-sensitivity corpus, **not an estimate of live-library
accuracy, provider retrieval recall or uptime**. It samples EPUB/TXT COPY intake;
uploads, MOVE, other parsers and the LLM are separate acceptance surfaces.

## Reproduce

From a source checkout with the frozen dependencies installed:

```bash
uv run python manage.py evaluate_intake --trial-threshold 0.80
uv run python manage.py evaluate_intake --trial-threshold 0.80 --output docs/design/data/intake-scorecard.json
```

The command starts a subprocess in an empty temporary working directory with a
new SQLite database and private storage. It does not load the caller's `.env`,
connect to its database/Redis/providers, or write its storage. The internal worker
refuses any pre-existing database or mismatched storage configuration before
migrating. Each scenario rolls back its database records, and generated COPY
sources are checked unchanged. DNS and network connections are blocked; an
unexpected attempt aborts the evaluation. Temporary data is removed on exit.

Baseline uses repository defaults (currently **0.86**), independent of the site's
environment settings. The trial changes the threshold in the isolated evaluator
only, including the production strong-match rule's threshold-dependent score.
All other rules and fixture responses are identical. The command changes no
production settings. The JSON contains every file outcome, ranked candidate
scores, provider failures, block reason, local-suggestion presence and grouped
Book/format counts. Corpus, rule-source and dependency-lock hashes identify the
inputs; Git CRLF/LF differences are normalized, and timestamps/UUIDs/latencies are
excluded. Tests run the complete experiment twice and require identical reports.

## Labels and counts

Each file names a correct work, an edition or explicitly unresolved edition, and
whether human review is mandatory. Every scenario includes a rationale. Work
correctness requires the labelled title and full author set, using punctuation/
case normalization but no fuzzy similarity. Edition correctness checks the
labelled ISBN, publisher, normalized language and year. Work-only labels require
no ISBN/publisher/year and an explicitly labelled language (empty if unresolved).

`correct_auto` requires correct work **and** edition, with no mandatory-review
label. `wrong_auto` is the union of wrong work, wrong edition and review-policy
violations; its diagnostic subcounts overlap. Review rate is actual open/claimed
review tasks divided by file count, not an assumption based on candidate score.
Provider-failure files have at least one persisted provider-error outcome; clean
no-results are separate. Scenario tags overlap, and a tagged sibling scenario
includes its contrasting partner; the per-file review flag is authoritative.

## Recorded experiment

The [complete scorecard](design/data/intake-scorecard.json) records:

| Threshold | Correct / wrong automatic | Review | Pipeline failures | Provider-failure files | Local suggestions |
| --- | --- | --- | --- | --- | --- |
| Baseline 0.86 | 16 / 1 | 16 / 33 (48.5%) | 0 | 2 | 7 |
| Trial 0.80 | 17 / 3 | 13 / 33 (39.4%) | 0 | 2 | 6 |

Issue #220 changed one label on purpose, not to improve the headline: the former
`edition-publisher-conflict` case is now `edition-publisher-other-printing`. Its
file and provider record differ only in the publisher of another printing; under
the E1 rule below it is accepted as the work, without that printing's ISBN,
publisher and year, instead of requiring review. It moved from the
`required-review` to the `work-only` group.

The lower threshold saves three reviews: one is a correct typo match; the other
two incorrectly accept a near-identical but distinct work title and a record with
a substituted coauthor. **This experiment does not justify lowering the default.**

The baseline mistake is deliberately retained as an adversarial control:
`sidecar-only-misleading` has no embedded identity, its body belongs to the Glass
Orchard, but its only sidecar and the provider record name the Long Return. Intake
trusts that internally consistent metadata and accepts the wrong work. Changing
the numeric threshold cannot distinguish this from truthful metadata. The corpus
exposes this identity-evidence limit; it does not claim that the current parser
reads an EPUB's body to verify its work.

Both work-only edition/translation cases correctly omit arbitrary edition data.
Local consensus without a provider record remains review-only, and path-only
evidence does not become a suggestion. The sibling-format case links EPUB/TXT to
one canonical Book and still runs a provider search for both files; the second
file's identical query reuses the first result (one provider call). Conflicting
sibling content remains held for review.

| Scenario group | Files | Baseline correct / wrong auto | Baseline review | Trial correct / wrong auto | Trial review | Provider-failure files B / T |
| --- | --- | --- | --- | --- | --- | --- |
| ambiguity | 1 | 0 / 0 | 1 (100.0%) | 0 / 0 | 1 (100.0%) | 0 / 0 |
| authors | 3 | 0 / 0 | 3 (100.0%) | 0 / 0 | 3 (100.0%) | 0 / 0 |
| coauthors | 3 | 2 / 0 | 1 (33.3%) | 2 / 1 | 0 (0.0%) | 0 / 0 |
| editions | 11 | 7 / 0 | 4 (36.4%) | 7 / 0 | 4 (36.4%) | 0 / 0 |
| isbn | 8 | 4 / 0 | 4 (50.0%) | 4 / 0 | 4 (50.0%) | 1 / 1 |
| local-consensus | 4 | 1 / 0 | 3 (75.0%) | 1 / 0 | 3 (75.0%) | 1 / 1 |
| no-isbn | 18 | 11 / 0 | 7 (38.9%) | 12 / 2 | 4 (22.2%) | 0 / 0 |
| provider-empty | 2 | 0 / 0 | 2 (100.0%) | 0 / 0 | 2 (100.0%) | 0 / 0 |
| provider-failure | 2 | 1 / 0 | 1 (50.0%) | 1 / 0 | 1 (50.0%) | 2 / 2 |
| required-review | 17 | 1 / 1 | 15 (88.2%) | 1 / 3 | 13 (76.5%) | 1 / 1 |
| sibling-formats | 4 | 3 / 0 | 1 (25.0%) | 3 / 0 | 1 (25.0%) | 0 / 0 |
| sidecars | 3 | 1 / 1 | 1 (33.3%) | 1 / 1 | 1 (33.3%) | 0 / 0 |
| threshold-boundary | 2 | 0 / 0 | 2 (100.0%) | 1 / 1 | 0 (0.0%) | 0 / 0 |
| work-only | 3 | 3 / 0 | 0 (0.0%) | 3 / 0 | 0 (0.0%) | 0 / 0 |

Extend this corpus with independently adjudicated, privacy-safe real imports
before making a deployment-wide calibration claim. Retain difficult examples and
provider-error cases; do not relabel errors to obtain a better headline number.


## Issue #220: end-to-end evaluation follow-up

The live evaluation in #220 (two samples of 100 real imports) found ten problems
(F1-F10). What changed, per finding; the named failing cases are regression tests in
`tests/test_issue220_intake.py`.

| Finding | Change | Named case |
| --- | --- | --- |
| F1-F3 placeholders | One placeholder vocabulary in `identification` (`is_placeholder_author`, `is_placeholder_title`, `PLACEHOLDER_METADATA_VALUES`); intake and the text-header parser use it. A sidecar is normalised once where it is read (`intake.normalize_sidecar_metadata`, option P1): placeholder authors/titles are dropped and listed under `sidecar_metadata.ignored`, so they reach neither `applied_fields`, nor conflicts, nor provider queries. A trailing or leading ` - Unknown` is never a title part or an author (`strip_placeholder_segments`); placeholder folders are no folder hints. | `2001 A Space Odissey - Unknown` |
| F4 (part) | An unenriched sidecar whose "author" is no name is Calibre's copy of a file-name split: title and author are ignored (`filename_split`). "12 - Title" is a numbered title without author. A record that lists *no* authors (an ISBN edition record) may take the authors of a same-title record that the file's own authors confirm (`auto_accept_authors_completed`); placeholders such as "Multiple Authors" are never completed. | `12 - Beasts Of Gor`, `Nightfall & Other Stories` |
| F5 name order | Names with the same tokens match whatever the order and comma (`Stross, Charles` = `Stross Charles` = `Charles Stross`); a comma-less "Surname Given" is also compared with the surname moved to the end. | `Accelerando` |
| F6 edition vs. work (E1) | A publisher of another printing is an *edition mismatch*: when title and author identify the work it no longer blocks; the work is accepted without that printing's ISBN, publisher and year, and the mismatch is recorded as `auto_accept_edition_mismatch`. ISBN and language mismatches, and every work conflict (other title or author), still block. | `Flinx in Flux` |
| F7 scoring and ties (S1/S2) | Records of one work are grouped before rivalry is judged (`candidates_group_as_one_work`): duplicates of one provider record collapse, and a record that only adds a contributor beside the same author (`["SparkNotes", "Aldous Huxley"]`) is no rival; among equal scores the record without extra contributors ranks first. Calibration: title and author weights are configurable (`BOOK_SCANNER_IDENTIFICATION_TITLE_WEIGHT` / `_AUTHOR_WEIGHT`, defaults 0.55 / 0.30). "Exact title + strong author" reaches the threshold through the strong-match rule, which keeps its provenance checks (partial file-name title, title and author from one hint, missing coauthor); raising the author weight instead would bypass them. A file without author evidence is never promoted. | `Brave New World`, `cry, the beloved country` |
| F8 cost (G2) | Providers run concurrently, each within a wall-clock budget (`BOOK_SCANNER_METADATA_PROVIDER_TIMEOUT_SECONDS`, default 20 s); a provider over budget is reported as an error. A provider failing `BOOK_SCANNER_METADATA_PROVIDER_BREAKER_FAILURES` times in a row (default 3) is skipped for `..._BREAKER_COOLDOWN_SECONDS` (default 120 s) with the reason "circuit open". Identical queries reuse the result for `..._CACHE_SECONDS` (default 900 s; 0 disables; failures are never cached). Once a finished provider returned a decisive candidate (file ISBN confirmed by the edition, or exact title + strong author against the file's own author evidence with no rival), the search stops waiting for the others, which are recorded as `skipped` (`BOOK_SCANNER_IDENTIFICATION_EARLY_EXIT`, default on). Breaker and cache state live in Redis when the queue uses it, otherwise in the process. Acceptance rules are unchanged; G3 (accept on local evidence first) was not implemented. | |
| F9 header credits (H1) | A header credit naming a domain, a site or imprint mark (a last word such as "Pool", "Books", "Library", "Press"), a placeholder, or a segment of the multi-part title is recorded under `text_header.rejected_authors` and never used as an author. | `The Last Battle` (`Caldron Pool`) |
| Path addendum | Intake records the whole path below the source root (`path_evidence`). Without a configured layout, the nearest folder written `Last, First` becomes an author *candidate* with provenance `path`: ranked after the file's own credits and before file-name guesses, sent to providers as the author when nothing better exists, and never accepted without a provider record naming that author. `Unknown/` is never an author. | `Herbert, Frank/Dune - House Atreides.txt` |

Still open: G3 (decide on local evidence first, providers as verification) needs a
maintainer decision on the "external verification is mandatory" principle; F10
(files with no author evidence at all) stays in review, and none of the changes
above make such a file auto-accept; H2 (spell-correcting credits such as "Issac
Asimov") and short stories of collections (addendum item 5) are not addressed.

### Measuring

Compare wall-clock import time separately from the sum of provider durations. Each
provider outcome records `status` (`results`, `no_results`, `error`, `skipped`),
its duration and whether the rows were `cached`; the worker logs
`metadata_provider_completed`, `_failed`, `_cached`, `_skipped` and
`metadata_provider_circuit_opened`.
