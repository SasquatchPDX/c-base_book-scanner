# Contribution recognition

Issue #27. The design and its reasoning are in
[design/27-contribution-recognition.md](design/27-contribution-recognition.md).
This page describes what is implemented (the MVP), how to run it, and what is not
tracked yet.

The feature is **off by default**. Nothing is recorded, and every page answers 404,
until `BOOK_SCANNER_CONTRIBUTIONS_ENABLED=true`.

## What earns points

Only **verified outcomes** earn points, and only after a hold period:

| Outcome | Default points | Recorded when |
|---|---:|---|
| Review resolved (accept candidate, manual entry, merge with existing book) | 3 | A member resolves a review in the web UI |
| Review marked unidentified | 1 | Same |
| Reviewing your **own** upload or scan | ×0.5 | The reviewer is the import's uploader or the scan's operator |
| Scan finished into the library | 5 + 0.05 per page of the *final* page set, at most 25 per job | Scan finalization completes and produces the cleaned library variant |

The scan bonus is split between members by their share of the final, active
pages (`ScanPageVersion.uploader`; pages without one count for the operator).

Not rewarded, on purpose: uploads, claiming a task, time spent, conversions,
downloads, and automatic resolutions (sibling formats linked without a reviewer).
No streaks.

**Daily soft cap.** Once a member has `BOOK_SCANNER_CONTRIBUTIONS_DAILY_SOFT_CAP`
(default 50) settled review points for one day, further review outcomes from that
day count at half weight.

## The ledger and settlement

`ContributionEvent` is an append-only ledger:

1. **Emitters** append a `PENDING` event with a snapshot of the outcome (the
   review's decision, decision time and book, or the scan's finish time and book).
   They run next to the existing transitions (`views.review_*`,
   `scan_pipeline.process_scan_finalization_job`) and never compute points. Each
   outcome has an idempotency key, so retries and reprocessing never double-count.
2. **Settlement** (`manage.py settle_contributions`, or the `settle_job` RQ job)
   compares each event's snapshot with the source rows:
   - outcome reversed while `PENDING` → `VOIDED` (no points, reason recorded);
   - outcome still standing after `BOOK_SCANNER_CONTRIBUTIONS_HOLD_DAYS`
     (default 14) → `SETTLED`, points computed now from the weight version that
     was current when the outcome happened;
   - outcome reversed after settlement → a separate negative `CLAWBACK` event; the
     settled row is never edited, so the ledger sums to zero for a reversed outcome.
3. Events older than `BOOK_SCANNER_CONTRIBUTIONS_RETENTION_MONTHS` (default 24;
   0 disables pruning) are folded into `ContributionMonthlyTotal` rows and
   deleted.

Settlement is idempotent. Run it daily: enable
`deploy/systemd/book-scanner-contributions.timer`, use cron, or press "Run
settlement now" on the admin page.

### What counts as a reversal

Because settlement checks the snapshot against the current rows, any change to a
resolved review is caught, whichever code path (or Django admin edit) made it:

- the review is no longer `RESOLVED` (reopened);
- it was decided again, by anyone (a new decision time or reviewer) — the newer
  decision gets its own event, the older one is voided or clawed back;
- the decision type or the resolved book changed, or the book was deleted;
- an admin flagged the decision as wrong;
- for scans: the job is no longer complete, its book changed or was deleted, or
  the cleaned variant was invalidated (for example by a rescan).

In addition, `ReviewDecisionCorrection` rows record *why* a decision stopped
standing where the code already reverses one:

| Source | Where it is recorded |
|---|---|
| `reopened` | "Edit search terms" on a resolved review |
| `re_resolved` | Accept / manual entry / merge / mark unidentified on an already resolved review |
| `book_deleted` | `book_deletion.delete_book_safely` (for each review that chose the book) |
| `admin` | "Flag a review decision as wrong" on the admin page |

## Pages

| URL | Who | What |
|---|---|---|
| `/me/contributions/` | the member | Private scorecard: season and all-time points, pending outcomes, "decisions held up" percentage, recent outcomes with reasons, open reviews they can pick up, leaderboard opt-in, export and erase |
| `/contributions/stats/` | signed-in members | Anonymous totals only: reviews resolved this month, books scanned this week/month, reviews waiting. Cached for 5 minutes |
| `/contributions/leaderboard/` | signed-in members | Opt-in board for the season (`?period=all` for all time). Also needs `BOOK_SCANNER_LEADERBOARD_ENABLED=true` |
| `/contributions/admin/` | admins (`AppUserProfile.role == admin`) | Adjust points, void an event, flag a review wrong, exclude/include a member on the leaderboard, edit weights, run settlement, export/erase a member's data |

Every page returns JSON unless the browser asks for HTML (`?view=html` or
`Accept: text/html`), like the rest of the app.

### Leaderboard

- Off unless `BOOK_SCANNER_LEADERBOARD_ENABLED=true` (and the feature flag).
- Members appear only after opting in on their scorecard, with explicit consent
  and a display handle (2–40 letters, digits, spaces, `.`, `_`, `-`; unique among
  members on the board, ignoring case). The account name is never shown unless a
  member types it as their handle.
- Leaving takes effect immediately; points stay on the private scorecard.
- Admins can exclude a member; the member keeps their scorecard.
- Seasons follow `BOOK_SCANNER_CONTRIBUTIONS_SEASON` (`month` or `quarter`); a
  season counts points settled since it started.
- The board shows "decisions held up" next to points.

## Admin controls

- **Weights** are versioned (`ContributionWeight`). Saving creates a new version;
  past events keep the version they were recorded with.
- **Adjust** adds a settled `ADMIN_ADJUSTMENT` (positive or negative) with a
  required reason. **Void** voids a pending event or claws back a settled one,
  also with a required reason. Nothing is edited silently; the actor is recorded.
- Django admin shows the ledger read-only.
- The ledger is never used to judge or ban members; bans stay in
  `AppUserProfile.is_disabled`, and nothing reads the ledger for them.

## Privacy (GDPR)

- Purpose: recognition, not evaluation (stated in the opt-in text and the
  export).
- Leaderboard visibility is consent-based (opt-in, revocable).
- **Export**: members download their own data as JSON from their scorecard;
  admins can export any member's data from the admin page.
- **Erase**: members erase their own contribution history (events, monthly
  totals, preference) from their scorecard; admins can do it for a member. Library
  records (reviews, scans) are not touched. An admin exclusion survives an erase
  so it cannot be used to get back on the board.
- **Retention**: detailed events are folded into monthly totals after
  `BOOK_SCANNER_CONTRIBUTIONS_RETENTION_MONTHS`.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `BOOK_SCANNER_CONTRIBUTIONS_ENABLED` | `false` | Master switch |
| `BOOK_SCANNER_LEADERBOARD_ENABLED` | `false` | Show the opt-in leaderboard |
| `BOOK_SCANNER_CONTRIBUTIONS_HOLD_DAYS` | `14` | Hold period before an outcome settles |
| `BOOK_SCANNER_CONTRIBUTIONS_DAILY_SOFT_CAP` | `50` | Settled review points per day before half weight (0 disables) |
| `BOOK_SCANNER_CONTRIBUTIONS_RETENTION_MONTHS` | `24` | Detail retention (0 keeps everything) |
| `BOOK_SCANNER_CONTRIBUTIONS_SEASON` | `month` | Leaderboard season: `month` or `quarter` |

## Not tracked yet

These parts of the design need prerequisites that do not exist in the codebase:

- **Metadata corrections on existing Books.** There is no Book edit workflow or
  edit history (only Django admin edits), so cleanup edits cannot be attributed
  or checked for reverts. Needs the `BookEdit` workflow (design phase 3).
- **Book merges.** There is no merge-two-Books operation; the
  `book_merged` correction source is reserved for it. (Deleting a Book is
  handled.)
- **ScanTailor stage work.** `ScanTailorStageState` has no user field, so stage
  edits are not attributed (design phase 3).
- **Rescans** (`ScanTodo` completion) and **new format variants** are not
  rewarded in this MVP; rescanned pages count once through the scan's final page
  set.
- **Spot-check queue** (weekly 5% `QUALITY_REVIEW` sampling) and **campaigns**
  are later phases. Admins can already flag a decision as wrong by hand.
- A **`verify_contributions`** command that recomputes the ledger from source
  tables is not implemented; settlement's snapshot comparison covers reversal
  detection.
- Outcomes from before the feature was enabled are not backfilled.
