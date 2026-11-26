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
| Baseline 0.86 | 15 / 1 | 17 / 33 (51.5%) | 0 | 2 | 8 |
| Trial 0.80 | 16 / 3 | 14 / 33 (42.4%) | 0 | 2 | 7 |

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
one canonical Book and still exercises the providers for both files. Conflicting
sibling content remains held for review.

| Scenario group | Files | Baseline correct / wrong auto | Baseline review | Trial correct / wrong auto | Trial review | Provider-failure files B / T |
| --- | --- | --- | --- | --- | --- | --- |
| ambiguity | 1 | 0 / 0 | 1 (100.0%) | 0 / 0 | 1 (100.0%) | 0 / 0 |
| authors | 3 | 0 / 0 | 3 (100.0%) | 0 / 0 | 3 (100.0%) | 0 / 0 |
| coauthors | 3 | 2 / 0 | 1 (33.3%) | 2 / 1 | 0 (0.0%) | 0 / 0 |
| editions | 11 | 6 / 0 | 5 (45.5%) | 6 / 0 | 5 (45.5%) | 0 / 0 |
| isbn | 8 | 4 / 0 | 4 (50.0%) | 4 / 0 | 4 (50.0%) | 1 / 1 |
| local-consensus | 4 | 1 / 0 | 3 (75.0%) | 1 / 0 | 3 (75.0%) | 1 / 1 |
| no-isbn | 18 | 10 / 0 | 8 (44.4%) | 11 / 2 | 5 (27.8%) | 0 / 0 |
| provider-empty | 2 | 0 / 0 | 2 (100.0%) | 0 / 0 | 2 (100.0%) | 0 / 0 |
| provider-failure | 2 | 1 / 0 | 1 (50.0%) | 1 / 0 | 1 (50.0%) | 2 / 2 |
| required-review | 18 | 1 / 1 | 16 (88.9%) | 1 / 3 | 14 (77.8%) | 1 / 1 |
| sibling-formats | 4 | 3 / 0 | 1 (25.0%) | 3 / 0 | 1 (25.0%) | 0 / 0 |
| sidecars | 3 | 1 / 1 | 1 (33.3%) | 1 / 1 | 1 (33.3%) | 0 / 0 |
| threshold-boundary | 2 | 0 / 0 | 2 (100.0%) | 1 / 1 | 0 (0.0%) | 0 / 0 |
| work-only | 2 | 2 / 0 | 0 (0.0%) | 2 / 0 | 0 (0.0%) | 0 / 0 |

Extend this corpus with independently adjudicated, privacy-safe real imports
before making a deployment-wide calibration claim. Retain difficult examples and
provider-error cases; do not relabel errors to obtain a better headline number.


## Provider lookup timing

A live lookup runs up to three configured providers concurrently. Requests within
each provider still follow its rate limits, timeouts and title fallback order. The
search waits for every provider outcome before ranking and applying the existing
acceptance rules. Equal scores and diagnostics retain configured provider order.
Compare wall-clock import time separately from the sum of provider durations.
