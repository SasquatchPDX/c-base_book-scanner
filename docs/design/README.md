# Design documents

These are design and scoping passes for issues marked *"[Not ready for
implementation] … needs its own design/scoping pass before anyone starts
coding"*. Each document follows the same structure:

1. the problem restated;
2. answers to every open question in the issue, each with a recommendation and
   the alternatives considered;
3. what is MVP and what comes later;
4. a data model sketch;
5. risks;
6. a test strategy;
7. a phased plan of issue-sized tasks with acceptance criteria.

Each document ends with a **Decisions to confirm** list. Implementation issues
are filed only after the owner confirms those decisions on the original issue.

| Doc | Issue | Recommendation in one line | Status |
|---|---|---|---|
| [26-local-llm-identification-signal.md](26-local-llm-identification-signal.md) | [#26](https://github.com/SasquatchPDX/c-base_book-scanner/issues/26) | The local LLM on the c-base AI server proposes *search hints* for files where every other signal failed. Real providers still supply candidates. It never auto-accepts, its output is schema-constrained and grounding-checked, and no book content goes into logs. | Proposed |
| [27-contribution-recognition.md](27-contribution-recognition.md) | [#27](https://github.com/SasquatchPDX/c-base_book-scanner/issues/27) | Points go only to *verified outcomes*, after a 14-day hold with voiding/clawback and a 5% admin spot-check. Everyone gets a private scorecard and the leaderboard is opt-in and members-only. Collective stats come first. | Proposed |
| [28-library-browse-at-scale.md](28-library-browse-at-scale.md) | [#28](https://github.com/SasquatchPDX/c-base_book-scanner/issues/28) | Search-first landing page with facets and shelves. PostgreSQL FTS and `pg_trgm` on a denormalized search row, behind a `SearchBackend` interface, with Meilisearch as the upgrade path. Keyset pagination and nginx-served thumbnails. Measured on 150k synthetic books. | Proposed |
| [32-audiobooks.md](32-audiobooks.md) | [#32](https://github.com/SasquatchPDX/c-base_book-scanner/issues/32) | Don't build audiobooks into this system now. Run Audiobookshelf next to it, skip audio cleanly in intake, and optionally cross-link later. A native data model is sketched in case the decision is ever reversed. | Proposed |

## Conventions

- File name: `<issue number>-<slug>.md`.
- Status values: *Proposed*, then *Accepted* (decisions confirmed on the
  issue), then *Implemented*, or *Superseded* (link to the replacement).
- Designs must respect [AGENTS.md](../../AGENTS.md):
  - Book is not a FileVariant;
  - originals and provenance are preserved;
  - slow work runs in background jobs;
  - storage access goes through the storage abstraction;
  - uncertainty becomes a review task;
  - no book contents in logs.
- Cite external sources inline and in a **Sources** section.
