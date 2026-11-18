# ScanTailor workspace event polling (#168)

Measured 18 November 2026 against PostgreSQL 17.10 in an isolated Docker container,
using Python 3.13 on Windows. The fixture has 300 pages, six stages per page, and
512 bytes of parameter provenance per stage. Twenty ticks follow an initial snapshot.
Each editor owns an independent event cursor, exactly as in the HTTP stream. The
benchmark executes those cursors sequentially; it measures server poll work, not
browser rendering or network delivery. Timings vary with host load and are evidence,
not CI performance thresholds.

## Results

Wall and database times below are medians per tick across all connected editors;
CPU is the mean process CPU time (the Windows CPU clock has coarse resolution).
Database time measures cursor execution including database/driver transfer, rather
than PostgreSQL server CPU alone. Rows are **returned to the application**, not
underlying heap/index rows visited.

| Activity | Editors | Version | Wall ms | CPU ms | DB execute ms | Rows returned | Full stages serialized |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| Idle | 1 | Before | 72.857 | 60.156 | 20.988 | 1801 | 1800 |
| Idle | 1 | After | 5.063 | 0.781 | 4.249 | 1 | 0 |
| Idle | 5 | Before | 378.234 | 393.750 | 97.353 | 9005 | 9000 |
| Idle | 5 | After | 24.989 | 4.688 | 20.964 | 5 | 0 |
| Editing | 1 | Before | 67.676 | 53.906 | 18.833 | 1801 | 1800 |
| Editing | 1 | After | 19.185 | 12.500 | 9.575 | 1802 | 1 |
| Editing | 5 | Before | 385.069 | 324.219 | 93.085 | 9005 | 9000 |
| Editing | 5 | After | 176.213 | 97.656 | 86.070 | 9010 | 5 |

The active case commits one stage revision change before every tick; every editor
receives it. The slowest five-editor completion after that commit was 774 ms before
and 205 ms after. With the default one-second interval, allow that scheduling interval
in addition to poll completion; these figures do not include browser/image fetch time.
Initial connections and reconnects still send all stages immediately, without waiting
for the next timer. The default stream reconnects after 45 ticks, so full snapshots
still have a periodic cost; the idle table intentionally isolates subsequent ticks.

## Implementation and limits

PostgreSQL computes an ordered fingerprint over stage `(id, revision, status,
updated_at)` values together with the job state/timestamp. An unchanged tick returns
one row without selecting, decoding or transferring parameters and artifact fields.
A changed fingerprint loads the narrow per-stage tokens and fetches full models only
for changed IDs. Each connection keeps its own tokens; no cross-process mutable cache
or new writer locks are involved. SQLite uses the portable narrow-token query directly.

The database still visits 1800 metadata rows per editor to compute the fingerprint:
this is not constant-time polling. At five idle editors, measured database execution
fell from 97 to 21 ms per tick, and wall time fell by 93%. A persisted job counter or
shared fanout system would remove more reads but would introduce writer coordination;
the measured remaining cost does not justify that larger change here. Revisit with
larger jobs or measured deployment pressure.

Comparing only `max(updated_at)` would be incorrect: an older timestamp can commit
after a newer one. The fingerprint covers every row, and changed-row comparison does
too. Tokens observed before payload fetch remain the cursor, so concurrent updates
cannot disappear between those queries. Mutations update `updated_at`; stage-input
rebinding now does so only when its values actually change. Creation/deletion and job
state changes also invalidate the cursor. Every reconnect starts with a fresh full
snapshot, preserving recovery behavior and the existing stage revision conflict guard.

## Reproduction and verification

Set `BOOK_SCANNER_DATABASE_URL` to an isolated PostgreSQL database whose role can create
pytest's test database, then run (PowerShell):

```powershell
$env:BOOK_SCANNER_BENCHMARK_WORKSPACE_EVENTS = 'true'
python -m pytest -s -q tests/benchmarks/test_workspace_events.py
```

The opt-in benchmark seeds only pytest's database. It retains the prior full-snapshot
implementation as the baseline, exercises one/five cursors idle/active, and prints
JSON measurements. `BOOK_SCANNER_BENCHMARK_WORKSPACE_EVENTS_MODE=before|after` selects
one side when investigating. It is skipped in normal CI.

`tests/test_workspace_events.py` covers 300-page idle queries, changed-stage-only
serialization, worker transitions, changes during fetch, creation/deletion, job
isolation, reconnects, and one/five authenticated HTTP streams that receive an initial
snapshot, idle heartbeats and incremental changes. The PostgreSQL integration test
holds an older transaction open until a newer change has already been delivered, then
verifies that its late commit is delivered too. Existing browser conflict and
Input/Result scenarios exercise the same event endpoint.
