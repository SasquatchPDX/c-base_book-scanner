# systemd units

Example units for running the web and worker processes on a single host:

- `book-scanner-web.service` runs Uvicorn with `ebook_server.asgi:application`
  on `127.0.0.1:8000`, behind the Nginx example in `deploy/nginx/`.
- `book-scanner-worker@.service` is a template for RQ workers. Start one or more
  instances (`book-scanner-worker@1`, `book-scanner-worker@2`, ...).
  It uses RQ's `RecoveryWorker` subclass to reconcile killed work horses with
  application job state and the human recovery queue; retain its `--worker-class`
  option when adapting the command.

### Sizing the worker pool (#195)

Image work (ScanTailor stages, page images) is CPU-bound and single-threaded per
worker: `ebook_server.rq_settings` pins OpenCV and NumPy/BLAS to one thread
(`BOOK_SCANNER_WORKER_THREADS`, default 1), so N workers use N cores and never
oversubscribe them. Run one worker instance per core, bounded by memory:

```sh
cores=$(nproc)
for i in $(seq 1 "$cores"); do sudo systemctl enable --now "book-scanner-worker@$i"; done
```

On the 2-vCPU / 3 GB reference VM that is two workers. A ScanTailor Output job on
a 300 dpi page needs a few hundred MB; conversions and OCR need more, so lower the
count (or `MemoryMax=` per unit) if the host also runs Calibre conversions.

A single-page ScanTailor edit is queued at the front of the queue, so it runs before
queued batch work ("Apply to all pages" is split into jobs of eight pages). No
separate queue or worker configuration is needed for that.

They assume:

- a dedicated unprivileged `book-scanner` user and group;
- the checkout at `/opt/book-scanner`, installed with `uv sync --frozen` so the
  units can call `.venv/bin/uvicorn` and `.venv/bin/rq` directly;
- the application configuration in `/opt/book-scanner/.env` (mode `0600`, owned
  by `book-scanner`). `settings.py` reads `.env` from the working directory, so
  no `EnvironmentFile=` is needed and `manage.py` commands run from the checkout
  see the same configuration as the services.

Edit the worker's `--url` to match `BOOK_SCANNER_REDIS_URL` (and the trailing
queue name if `BOOK_SCANNER_QUEUE_NAME` is changed). If library storage is an
NFS/SMB mount, add `RequiresMountsFor=<storage root>` to both units' `[Unit]`
section so they never start against an unmounted path. Adjust or remove the
`postgresql.service` / `redis-server.service` ordering when those services run
on other hosts.

The worker template also bounds its whole cgroup: memory pressure begins at 4 GiB,
the hard memory ceiling is 6 GiB, at most 128 tasks may run, and CPU is limited to
two cores' worth of time. These ceilings include all RQ work horses and converter
descendants, and affect other jobs assigned to the instance too. Tune them with a
systemd override for the site's accepted workload. Converter-specific byte limits
and an optional existing quota-backed local scratch root are configured in `.env`;
see "Workers and external tools" in `docs/operations.md`. The application samples
aggregate scratch growth; a filesystem quota supplies the strict disk boundary.

Install and start:

```bash
sudo cp deploy/systemd/book-scanner-web.service \
  'deploy/systemd/book-scanner-worker@.service' /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now book-scanner-web book-scanner-worker@1
journalctl -u book-scanner-web -u 'book-scanner-worker@*' -f
```

Run `migrate` and `collectstatic` as the `book-scanner` user before the first
start and after every upgrade, then restart the units. See the "Deployment
walkthrough" in [docs/operations.md](../../docs/operations.md).

Optional: `book-scanner-contributions.service` and `.timer` run the daily
contribution settlement (`manage.py settle_contributions --enqueue`). They only
do work when `BOOK_SCANNER_CONTRIBUTIONS_ENABLED=true`; see
[docs/contributions.md](../../docs/contributions.md).

Recommended: `book-scanner-scan-prune.service` and `.timer` run the hourly sweep of
ScanTailor-Web stage files no stage row refers to any more
(`manage.py prune_scan_artifacts --enqueue`, #204); see "Disk use" in
[docs/scantailor-web.md](../../docs/scantailor-web.md).
