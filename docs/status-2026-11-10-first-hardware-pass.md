# Status update — first pass before hardware testing (2026-11-10)

## Summary

Before the first test on real scanner hardware, the codebase was reviewed for
errors and showstoppers. The baseline was green (186 tests, mypy, ruff and
frontend checks all clean), so the problems were in paths the tests did not
exercise: real HTTP clients, real camera images, and real OCR, queue and storage
behaviour.

Four parallel reviews covered:

- scan capture and finalization
- ScanTailor image processing
- OCR and background workers
- intake, identification, auth and storage

Findings were spot-checked against the code, then fixed in isolated branches,
merged, and pushed to `master` (`ab322f7..e689195`, 10 commits).

**Results**

- Tests went from 186 to 292 passing (8 infrastructure tests skipped locally).
- mypy (strict), ruff lint and format, Django checks, the migration check, and the
  TypeScript checks and tests are clean.
- An end-to-end HTTP smoke test ran against a throwaway server with 6000×4000
  camera-style JPEGs. It covered login, upload of a left/right spread, and finish
  (routed to bibliographic review).
- Spreads split exactly at the gutter (x=3000 of 6000, in about 0.05 s).
- A JPEG with EXIF orientation 6 loads upright (4000×6000) and is correctly not
  split.
- The infrastructure suite (`scripts/run_infrastructure_tests.sh`, with real
  Postgres, Redis, LDAP and nginx) was **not** run.

## Showstoppers fixed

- **Deskew made pages worse.** It rotated in the wrong direction, doubling the
  tilt, and filled corners with red on RGB images.
- **Common camera image types broke processing.**
  - EXIF orientation was ignored, so a portrait photo was split as a two-page
    spread.
  - CMYK JPEGs crashed the first stage.
  - 16-bit PNG and TIFF images came out all white.
- **Page split picked the wrong column**, often about 10% off the gutter, which
  cuts text. It has been rewritten in numpy and reports a confidence value.
- **Page replacement (PUT) could not work for a real client.** Django parses
  multipart bodies only for POST. The existing tests passed only because of a
  quirk in Django's test client.
- **OCR gave up after about 8 seconds.** The default was 40 polls at 0.2 s. It is
  now a wall-clock deadline of about 25 minutes with backoff, and it tolerates
  unknown statuses and transient 5xx or transport errors.
- **Failed jobs were never retried.** RQ 2.0 schedules retries that have an
  interval, and those only run on a worker started with `--with-scheduler`.
  Without that flag, finalization stayed in FINALIZING forever. The flag is now
  documented, and retries fall back to immediate when no scheduler is running.

## Other fixes

### Scanner capture API

- Unknown `side` values return 400 instead of silently overwriting the other page.
- API clients get a 401 JSON response instead of an HTML login redirect.
- Uploads are validated as real images, and the MIME type is sniffed from the
  file.
- The client filename and extension are preserved.
- The file copy happens outside the job-row lock.
- The first upload moves the job from OPEN to CAPTURING.
- Pause, resume and cancel are idempotent and lock the job row.
- Epoch and millisecond capture timestamps are accepted.
- A storage outage returns a retryable 503.

### Thumbnails

- Thumbnails are cached, downscaled JPEGs with EXIF orientation applied, instead
  of the full original.

### Image processing

- Thresholds are computed per image.
- Small specks no longer expand the content box.
- An unreadable image fails the stage instead of producing a fake success.
- Image dimensions no longer go stale after a parameter change.
- A running job can no longer overwrite a newer user edit.
- Stage outputs are named `.png`.
- The Fox, Window and Wolf binarizations are corrected.
- DPI is preserved.

### OCR and workers

- Zero OCR outputs is now a failure.
- Plain `content` is no longer base64-decoded.
- Uploads to the OCR service have a longer submit timeout, and OCR jobs have a
  2-hour job timeout.
- RUNNING jobs left behind by a dead worker can be re-entered, and there is a
  new `recover_stale_jobs` command.
- The scan PDF is streamed to a file instead of being built in memory, and pages
  with alpha are flattened.
- A failed enqueue marks the job as failed.
- Duplicate OCR jobs are deduplicated.
- Converter subprocesses have a timeout and include the tail of stderr in errors.

### Intake and identification

- The folder name is no longer added to Open Library queries. It caused zero
  hits.
- ISBN lookups keep the queried ISBN and resolve author names.
- Failed imports retry properly.
- Unchanged, already-imported files are not re-ingested.
- Half-written, hidden and temporary files are skipped.
- EPUBs are searched for an ISBN across all identifiers.
- The User-Agent is configurable, and 429 responses are handled.

### Auth and security

- LDAP TLS validates server certificates. Before this, any certificate was
  accepted.
- Usernames are case-insensitive, so a disabled user cannot log back in with
  different capitalisation.
- Production LDAP configuration is validated at startup.

### Storage

- If the share is not mounted, writes fail instead of filling the local disk.
- An optional sentinel file can confirm the share is mounted.
- The readiness check performs a real test write.
- Copies use `copyfile`, which is safe on CIFS and NFS.
- Stale NFS handles return 503.

### Database

- `storage_identifier` columns are widened to 512 characters (migration 0016).
- Filenames are capped, and titles are truncated to their column limits.

### Admin

- New command `promote_admin <uid> [--staff] [--superuser]`. Before this there
  was no way to create the first admin.

## Before testing tonight

1. Run `uv run python manage.py migrate` to apply migration 0016.
2. Start RQ workers with `--with-scheduler`:

   ```bash
   uv run rq worker --with-scheduler --config ebook_server.rq_settings --url redis://localhost:6379/0 ebook-server
   ```

3. LDAP certificates are now validated. If the directory uses a private CA, set
   `BOOK_SCANNER_LDAP_CA_FILE`. For a lab directory only, you can opt out with
   `BOOK_SCANNER_LDAP_TLS_REQUIRE_CERT=false`.
4. Production refuses to write unless the storage root already exists. Optionally
   run `touch <storage-root>/.ebook-server-root` and set
   `BOOK_SCANNER_STORAGE_SENTINEL_FILE=.ebook-server-root`.
5. Create the first admin with
   `uv run python manage.py promote_admin <your-uid> --superuser`.
6. The scanner device logs in as an LDAP member and must send a CSRF token and
   `Referer` header on every unsafe request. Over HTTPS, its origin must be listed
   in `BOOK_SCANNER_CSRF_TRUSTED_ORIGINS`. The full steps are under "Client
   recipe" in `docs/scanner-protocol.md`.
7. Smoke-test the server with the reference client before connecting the
   hardware:

   ```bash
   uv run python scripts/scanner_client_example.py --base-url <url> \
     --username <uid> --password <pw> --directory <images> [--spreads] \
     [--finish --allow-raw-pages]
   ```

8. `finish` still rejects raw, unprocessed pages unless the request sends
   `allow_raw_pages=true`, by design. The 400 response now lists the missing
   pages and gives a hint.

### New optional settings (`BOOK_SCANNER_` prefix)

| Area | Settings |
| --- | --- |
| LDAP | `LDAP_CA_FILE`, `LDAP_TLS_REQUIRE_CERT`, `LDAP_USERNAME_ATTRIBUTE` |
| Storage | `STORAGE_REQUIRE_EXISTING_ROOT`, `STORAGE_SENTINEL_FILE` |
| Intake | `SOURCE_MIN_AGE_SECONDS`, `INGEST_MAX_AUTO_ATTEMPTS`, `METADATA_USER_AGENT` |
| OCR | `OCR_POLL_DEADLINE_SECONDS`, `OCR_POLL_INTERVAL_START_SECONDS`, `OCR_POLL_INTERVAL_MAX_SECONDS`, `OCR_SUBMIT_TIMEOUT`, `OCR_JOB_TIMEOUT_SECONDS` |
| Conversion | `CONVERTER_TIMEOUT_SECONDS` |

All of these have working defaults and are documented in `.env.example` and
`docs/operations.md`.

## Not verified or deferred

- **OCR service contract.** It has not been tested against the real OCR server.
  The client assumes `POST /v1/ocr/jobs` and `GET /v1/ocr/jobs/{id}`. Do one
  manual `curl` against the real service first.
- **Real Redis/RQ.** The scheduler detection and retry fixes have only been
  tested with fake queues.
- **Scan job list and detail performance.** These endpoints run about 6 queries
  per page, roughly 1,900 per poll for a 300-page book. This is not fixed.
- **Plain `ldap://` without StartTLS** is still allowed in production, which sends
  passwords in cleartext. It was left in place so it cannot block startup.
- **Stale RUNNING finalization jobs** still block re-dispatch until
  `recover_stale_jobs --apply` is run.
- **Scanner device credentials** (Sprint020 H), login throttling (Sprint020 I),
  curvature dewarping and zone segmentation (Sprint020 A/B) remain planned work.
- The output stage's `keep_color` option is still effectively ignored.
