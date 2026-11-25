# Physical scanner acceptance run (#169)

**Status: not yet run.** No run on the real scanner has been recorded. The
simulated-device journey runs in CI (`tests/browser/scantailor-concurrency.spec.ts`,
"a scanner capture survives a lost acknowledgement and a member re-finishes its
rescan"); this file records the separate site run.

The procedure is "Recording the physical scanner acceptance run" in
`docs/operations.md`. Add one section per run, newest first, using the template
below. Do not include the scanner key or the member session.

## Template

### Run of YYYY-MM-DD

- Operator:
- Staging origin:
- Server commit:
- Scanner firmware / sans version:
- Onboarding check: `scripts/check_scanner_onboarding.sh --pi` result
- Interrupted upload (step 3): how it was interrupted, which page, what sans did
- Local deletion (step 4): sans kept its captures until the `verified: true`
  response arrived (yes/no, and what was observed)

Recorder output (`scripts/record_scanner_site_run.py`, exit status 0):

```json
{
  "status": "passed",
  "job_id": "YYYYMMDDTHHMMSSZ",
  "...": "paste the full record here"
}
```
