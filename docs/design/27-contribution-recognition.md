# Design 27: recognising review, scan and cleanup contributions (points, scorecards, leaderboard)

- **Issue:** [#27](https://github.com/SasquatchPDX/c-base_book-scanner/issues/27)
- **Status:** Proposed. Waiting for the owner to confirm the decisions in
  [Decisions to confirm](#decisions-to-confirm).
- **Related:** #13 (batch review for bulk imports), #26 (the LLM assist must
  not inflate points), #28 (cleanup campaigns need facets), #55 (scanner
  workflow).

## 1. Problem

Reviewing imports, scanning physical books and cleaning up metadata is volunteer
work, and it has to be recruited actively. The issue proposes points, scorecards
and a leaderboard so members can see the effort (their own and other people's)
and pick up work.

The risk is spelled out in the issue and in AGENTS.md. A system that rewards
*volume* rewards fast, careless "accept candidate" clicks, which is exactly what
the review workflow exists to prevent ("weak matches must not be silently
accepted"). The project also has no per-user activity data today. A
contribution log is new personal data about members of a German hackerspace,
so the GDPR (DSGVO) applies.

## 2. What exists today

The current models already capture most of what is needed, but no aggregation
reads it:

| Contribution | Where it is recorded today |
|---|---|
| Review resolved | `ReviewTask.reviewer`, `decision`, `decided_at`, `resolved_book`, `selected_candidate` |
| Upload | `ImportJob.uploader`, `FileVariant.uploader` |
| Scan capture | `ScanJob.operator`, `ScanPageVersion.uploader`, `version`, `is_active` |
| Rescan | `ScanTodo.creator`, `claimed_by`, `state`. The replacement `ScanPageVersion.uploader`. |
| ScanTailor stage work | `ScanTailorStageState` has **no user field**: edits are not attributed |
| Metadata cleanup on an existing Book | **No edit workflow exists.** Only admin and Django admin edits, with no history. |

Two gaps follow from this table:

- Nothing records that a review decision was **later reversed or corrected**,
  so "points for outcomes that hold up" cannot be computed yet.
- Book metadata edits have no audit trail.

## 3. Answers to the open questions

### Q1. What earns points?

**Recommendation: points for *verified outcomes*, settled after a hold period,
with weights set by admins.** Raw actions never earn points.

Each contribution creates a `ContributionEvent` in state `PENDING`. A daily
settlement job moves it to `SETTLED` once the hold period has passed (default
14 days) and the outcome still stands. If the outcome is reversed first, the
event becomes `VOIDED`. A reversal after settlement adds a negative `CLAWBACK`
event, so the ledger is append-only and can be audited.

The defaults below are a starting point and all can be configured:

| Outcome | Default points | Counts when | Voided / clawed back when |
|---|---:|---|---|
| Review resolved: accepted candidate, merged into an existing Book, or manual entry | 3 | The task is still RESOLVED with the same `resolved_book` after the hold period | The task is reopened, the Book is merged away or deleted as a wrong match, or an admin flags the decision as wrong |
| Review: marked unidentified | 1 | Same, and the task is not later resolved by someone else with a real match | Someone else identifies it (that person gets the full points) |
| Review of **own upload** | ×0.5 | Normal users may resolve their own uploads (AGENTS.md). Half weight removes the incentive to farm. | Same as above |
| Scan job finished into the library | 5 + 0.05 per page in the *final* page set, capped at 25 per job | `ScanJob` COMPLETE and a Book with a FileVariant from it | The Book or variant is deleted as junk or a duplicate |
| Rescan completed | 0.5 per page | `ScanTodo` COMPLETED and the replacement version is still active | The page is rescanned again for the same reason within the hold period (the original rescan didn't fix it) |
| ScanTailor stage pages approved | 0.1 per page per job | The job's output is finalized | — (needs stage attribution first, phase 3) |
| New format variant (not a duplicate) | 1 | The SHA-256 is unique and the variant is linked to an identified Book after the hold period | Deleted, or found to be a broken file |
| Metadata cleanup on an existing Book | 1 per accepted edit set | The edit is not reverted during the hold period | Reverted (needs the edit workflow, phase 3) |

Not rewarded, on purpose:

- **Uploads in themselves.** A bulk dump is not a contribution until it is
  identified, and rewarding it would reward junk.
- **Time spent and actions per hour.** These are trivially gamed and amount to
  surveillance.
- **Claiming a task.**
- **Conversions and downloads.**

### Q2. Perverse incentives: how to avoid rewarding careless acceptance

1. **The hold period and voiding** (Q1) turn careless accepts into zero
   points.
2. **A quality sample.** Every week the settlement job puts a random 5% of
   settled-eligible review decisions (at least 3 per active reviewer) into an
   admin "spot-check" queue. This is a `ReviewTask` with
   `task_type=QUALITY_REVIEW`, which already exists in the model. A decision the
   spot-checker marks wrong is voided or clawed back, and the reviewer sees the
   feedback on their own scorecard. This is how quality is measured without
   waiting for someone to stumble on a wrong book.
3. **Accuracy shown next to points.** The personal scorecard and the
   leaderboard show "decisions held up: 97%" next to the points. A leaderboard
   column people care about makes care visible.
4. **A daily soft cap.** After 50 settled review points in one day, further
   events that day count at 0.5×. The work still gets done, but there is no
   bonus for racing. The cap can be configured.
5. **Points for LLM assistance do not grow.** A review resolved with a #26
   suggestion earns the same as any other. The suggestion is not the
   reviewer's work, and the review still has to be checked.
6. **No streaks and no countdowns.** Streak mechanics push people to do
   something every day, whatever its quality. Leave them out.

### Q3. Scope and visibility: opt-in? public leaderboard?

**Recommendation: three levels, most private first.**

- **Personal scorecard, always on.** Only the member sees it. It shows their
  settled and pending points, accuracy, and the open tasks they could pick up.
  It is generated from their own data, which they can already see.
- **Leaderboard, opt-in per member.** It is shown only to authenticated
  members, never to anonymous visitors. Each member enters a display handle.
  Showing their LDAP uid is an explicit choice, never the default. A member can
  leave at any time. Leaving removes them from the board immediately.
  Their points stay in their private scorecard.
- **Collective stats, always on, no names.** "Members resolved 312 reviews this
  month", "4 books scanned this week", and progress bars for campaigns ("Help
  clean up the German sci-fi pile: 120/400"). This gives most of the motivation
  with no personal exposure. For a hackerspace it is probably the most useful
  part.

The leaderboard covers a season (calendar month or quarter, set by admins) plus
all time. A season leaderboard keeps newcomers from facing a permanent wall of
veterans.

**GDPR notes:**

- The legal basis for the opt-in leaderboard is consent (Art. 6(1)(a)).
- The private ledger is needed to run the feature and is minimal.
- State the purpose in the opt-in text: recognition, not evaluation of
  members.
- Retention: delete detailed events older than 24 months and keep monthly
  aggregates only.
- An admin action exports or erases one member's contribution data on request.
- The data is never used to judge members or for bans. Bans stay in
  `AppUserProfile.is_disabled`, and nothing reads the ledger for them.

### Q4. New domain objects

See section 4. The objects are:

- `ContributionEvent`, an append-only ledger;
- `ContributionWeight`, admin-configurable points;
- `ContributorPreference`, the opt-in and display handle;
- `ReviewDecisionCorrection`, a record that a decision was overturned;
- optionally `Campaign`.

Leaderboards and scorecards are aggregate queries over settled events, cached
in Redis for 5 minutes. At hackerspace scale (hundreds of members, tens of
thousands of events) no materialized table is needed.

### Q5. Scan-specific weighting

**Recommendation: a flat completion bonus, plus per-page points for the *final*
page set, capped.** The bonus is 5 points. Pages earn 0.05 each and the job
total is capped at 25 points, so a 400-page book is worth 25 points.

- **Not time-based.** Time is unmeasurable from the server, invites idling,
  and is surveillance.
- **Not raw captured pages.** Counting captures would reward scanning
  carelessly and rescanning.
- Pages are counted from the *active* `ScanPageVersion`s in the finalized job,
  so a page that needed a rescan still counts once.
- The operator who does a rescan earns the rescan points, which shares work
  fairly when several members hand a book over between them.
- When several operators work on one job, the completion bonus is split by each
  operator's share of the final active pages (`ScanPageVersion.uploader`).

## 4. Data model changes (sketch)

```python
class ContributionKind(models.TextChoices):
    REVIEW_RESOLVED = "review_resolved"
    REVIEW_UNIDENTIFIED = "review_unidentified"
    SCAN_COMPLETED = "scan_completed"
    RESCAN_COMPLETED = "rescan_completed"
    STAGE_APPROVED = "stage_approved"  # phase 3
    VARIANT_ADDED = "variant_added"
    METADATA_EDIT = "metadata_edit"  # phase 3
    CLAWBACK = "clawback"
    ADMIN_ADJUSTMENT = "admin_adjustment"


class ContributionEvent(models.Model):
    """Append-only. Never updated except state PENDING -> SETTLED/VOIDED by the settlement job."""

    id = UUIDField(primary_key=True, default=uuid4)
    user = FK(User, on_delete=CASCADE, related_name="contribution_events")
    kind = CharField(choices=ContributionKind.choices)
    state = CharField(choices=["pending", "settled", "voided"])
    points = DecimalField(max_digits=7, decimal_places=2)  # computed at creation from weights
    weight_version = PositiveIntegerField()  # which weight table applied
    # Generic subject reference without GenericForeignKey overhead:
    review_task = FK(ReviewTask, null=True, on_delete=SET_NULL)
    scan_job = FK(ScanJob, null=True, on_delete=SET_NULL)
    scan_todo = FK(ScanTodo, null=True, on_delete=SET_NULL)
    file_variant = FK(FileVariant, null=True, on_delete=SET_NULL)
    book = FK(Book, null=True, on_delete=SET_NULL)
    reverses = FK("self", null=True, on_delete=PROTECT)  # for CLAWBACK
    reason = CharField(max_length=255, blank=True)  # void/clawback/adjustment reason
    actor = FK(User, null=True, related_name="+")  # admin for adjustments
    occurred_at = DateTimeField()
    settle_after = DateTimeField()
    settled_at = DateTimeField(null=True)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["user", "kind", "review_task"],
                condition=Q(kind__in=[...]),
                name="one_review_event_per_task_user",
            ),
            ...,
        ]  # idempotent emitters
        indexes = [
            Index(fields=["state", "settle_after"]),
            Index(fields=["user", "state", "occurred_at"]),
        ]


class ContributionWeight(models.Model):  # versioned; admin edits create a new version
    version = PositiveIntegerField()
    kind = CharField()
    base_points = DecimalField()
    per_unit_points = DecimalField(default=0)
    cap = DecimalField(null=True)
    own_upload_multiplier = DecimalField(default=Decimal("0.5"))
    active_from = DateTimeField()


class ContributorPreference(models.Model):
    user = OneToOneField(User, primary_key=True, on_delete=CASCADE)
    leaderboard_opt_in = BooleanField(default=False)
    display_handle = CharField(max_length=40, blank=True)  # validated, unique among opted-in
    opted_in_at = DateTimeField(null=True)


class ReviewDecisionCorrection(models.Model):
    """A later finding that a resolved ReviewTask's decision was wrong (by spot-check, merge, or reopen)."""

    review_task = FK(ReviewTask, on_delete=CASCADE)
    found_by = FK(User, null=True)
    source = CharField(choices=["spot_check", "reopened", "book_merged", "book_deleted", "admin"])
    note = TextField(blank=True)
    created_at = DateTimeField(auto_now_add=True)


# Prerequisites (phase 3):
# ScanTailorStageState.edited_by = FK(User, null=True)   # attribution of stage edits
# BookEdit(book, editor, before JSON, after JSON, state, reverted_by) # metadata-cleanup workflow + history
```

- **Emitters** are small functions called next to the existing state
  transitions, for example in `views.review_accept_candidate`, the scan
  finalization in `scan_pipeline.py`, and `ScanTodo.complete`. They are
  idempotent (unique constraints), so retries and reprocessing never
  double-count.
- **Settlement** is a daily RQ-scheduled job. It is the same pattern as
  `recover_stale_jobs`, with a management command for manual runs.
- **Reversal detection** reacts to `ReviewDecisionCorrection` rows and to Book
  deletion and merge, through `book_deletion.py` and the merge path.

## 5. Admin controls

- Edit the weights; each edit creates a new version and does not change past
  events.
- Turn off the whole feature, or only the leaderboard (settings flags, both
  default off for the leaderboard).
- Void or adjust an event, with a required reason. This creates an
  `ADMIN_ADJUSTMENT` or `CLAWBACK` and is never a silent edit.
- Exclude a user from the leaderboard, for example after abuse. The user still
  sees their scorecard.
- Start or end a season.
- Export or erase one member's contribution data (GDPR).
- A spot-check queue with a "decision wrong / correct" action.
- Create a campaign with a saved library filter (#28) and a target count.

All admin actions use the existing role check (`AppUserProfile.role == ADMIN`).
LDAP stays only the authentication source, and the leaderboard never reads LDAP
attributes such as the real name.

## 6. Risks

| Risk | Mitigation |
|---|---|
| Careless fast reviews | Points only after the hold period, voiding, a 5% spot-check, accuracy shown, a daily soft cap |
| Self-dealing (upload junk, then review it yourself) | Uploads earn nothing. Own-upload reviews earn half. Junk deleted later is clawed back. |
| Collusion (two members reviewing each other's uploads) | Spot-check sampling, and an admin report of reviewer/uploader pair frequency |
| Social pressure or exclusion in a volunteer space | Leaderboard opt-in, seasonal, members only. Collective stats are the default motivator. |
| Privacy / GDPR | Opt-in consent, minimal data, 24-month retention, export and erase, purpose limitation (never used to judge members) |
| Ledger drift from reality | Append-only events with source references. A `verify_contributions` command recomputes from source tables and reports differences. |
| Gaming via the #26 LLM assist | No bonus for AI-assisted reviews. Spot-check covers them. |
| Performance | Tiny data volumes. 5-minute cached aggregates. |

## 7. Test strategy

- **Unit tests:**
  - weight calculation, including the scan page cap, own-upload multiplier and
    daily soft cap;
  - settlement state machine transitions;
  - clawback symmetry: the sum of points after a reversal equals 0;
  - weight versioning, where old events keep their points.
- **Integration tests:**
  - each emitter fires once, even under retries (idempotency constraints);
  - a reopened review voids the pending event;
  - a Book deletion claws back settled points;
  - the spot-check queue sampling respects the per-reviewer minimum.
- **Permission and privacy tests:**
  - an opted-out member never appears in any leaderboard response (HTML or
    JSON);
  - normal users can't see other members' scorecards;
  - the erase action removes events and preferences;
  - admin-only endpoints are blocked for normal users.
- **E2E (Playwright):** a member resolves a review, sees the pending points,
  settlement runs (a time-travel fixture), and the points show as settled. An
  opt-in or opt-out toggle updates the board.

## 8. MVP and later

- **MVP (phases 1 to 2):**
  - the ledger with emitters for review resolution and scan completion;
  - the hold period and settlement;
  - voiding on reopen, merge or delete;
  - the personal scorecard;
  - anonymous collective stats on the library home page;
  - admin weights and the feature flag.
- **Phase 2b:** an opt-in members-only leaderboard with seasons, the spot-check
  queue, and accuracy display.
- **Later:**
  - stage-edit attribution and the metadata-edit workflow with history
    (prerequisites for cleanup points);
  - campaigns tied to #28 saved filters.

Badges are possible but not planned.

## 9. Phased implementation plan (issue-sized tasks)

1. **Ledger models and settlement.**
   - Scope: `ContributionEvent`, `ContributionWeight` with seed defaults,
     `ReviewDecisionCorrection`, the migration, the settlement job and command,
     and read-only Django admin views.
   - Acceptance:
     - the state machine is tested;
     - weights are versioned;
     - settlement is idempotent;
     - the feature flag is off by default.
2. **Review and scan emitters.**
   - Scope: emitters in review accept, merge, manual entry and mark
     unidentified, in scan finalization, and in `ScanTodo.complete`; voiding on
     reopen, Book delete and merge.
   - Acceptance:
     - one event per outcome per user, even after reprocessing;
     - every reversal path voids or claws back;
     - `verify_contributions` reports zero differences on test fixtures.
3. **Personal scorecard and collective stats.**
   - Scope: a `/me/contributions/` page and a home-page "this month" strip with
     no names.
   - Acceptance:
     - only the owner sees the scorecard;
     - the collective numbers match the ledger;
     - at most 5 queries are made, with caching.
4. **Opt-in leaderboard and seasons.**
   - Scope: `ContributorPreference`, opt-in with consent text, a display handle,
     the season and all-time board, and admin exclusion.
   - Acceptance:
     - opted-out members never appear (a test across HTML and JSON);
     - the handle is validated and unique;
     - leaving takes effect immediately.
5. **Quality spot-check.**
   - Scope: weekly sampling into `QUALITY_REVIEW` tasks, the admin
     "wrong/correct" action that creates corrections, and accuracy on the
     scorecard and board.
   - Acceptance:
     - sampling rates are respected;
     - a "wrong" verdict claws back the points;
     - the reviewer sees the feedback.
6. **GDPR tooling.**
   - Scope: retention pruning to monthly aggregates and admin export and erase
     per user.
   - Acceptance:
     - pruning keeps the aggregates correct;
     - export is JSON;
     - erase is complete (test).
7. *(Later)* `ScanTailorStageState.edited_by` and stage points, then the
   `BookEdit` workflow with history and cleanup points, then campaigns.

## Decisions to confirm

1. Points come only from verified outcomes after a 14-day hold, with
   voiding/clawback. Uploads, time and claims earn nothing (Q1, Q2).
2. A private scorecard for everyone, a members-only leaderboard that is
   **opt-in** and seasonal, and anonymous collective stats (Q3).
3. Scans earn a flat bonus plus capped per-page points from the final page set,
   with no time component (Q5).
4. An admin spot-check of about 5% of review decisions is acceptable as a
   workload.
5. A data retention period of 24 months for detailed events.

## Sources

- AGENTS.md: "Manual Review", "Permissions", "Human Review Is a First-Class
  Workflow".
- GDPR Art. 6(1)(a) consent and Art. 17 erasure: <https://gdpr-info.eu/art-6-gdpr/>, <https://gdpr-info.eu/art-17-gdpr/>
- Goodhart's law in metric design ("When a measure becomes a target, it ceases
  to be a good measure"): <https://en.wikipedia.org/wiki/Goodhart%27s_law>
- Wikipedia's own experience with edit-count "editcountitis" as a cautionary
  example of volume metrics: <https://en.wikipedia.org/wiki/Wikipedia:Editcountitis>
- OpenStreetMap / MapRoulette challenges, a model for collective cleanup
  campaigns: <https://maproulette.org/>
