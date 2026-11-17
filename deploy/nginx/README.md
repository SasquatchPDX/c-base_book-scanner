# Nginx reverse proxy

`book-scanner.conf` is a production-oriented HTTP upstream example for an HTTPS
terminator or ingress. Point `book-scanner-web:8000` at the Uvicorn service and
keep the dedicated ScanTailor event-stream location unbuffered. Its 75-second
timeouts exceed the default 45-second application stream lifetime.

The application trusts forwarded scheme information only when
`BOOK_SCANNER_SECURE_PROXY_SSL_HEADER=true`. Restrict direct access to Uvicorn so
clients cannot forge trusted proxy headers. Configure TLS, certificates, host
names, and request-size policy to match the site.

Nginx, not Uvicorn, serves `/static/`. Run `manage.py collectstatic --noinput
--clear` and publish the resulting `src/staticfiles/` directory at the `alias`
path (`/srv/book-scanner/static/`), or point the `alias` at it directly; see
"Static files" in `docs/operations.md`. When Uvicorn runs on the same host,
change the upstream to `127.0.0.1:8000`. The server forwards `Host $host`, which
omits the port: if it listens on a non-default port such as 8080, add the
browser origin (for example `http://192.0.2.10:8080`) to
`BOOK_SCANNER_CSRF_TRUSTED_ORIGINS`.

`integration.conf` is only for the Docker-backed acceptance suite. It connects a
pinned Nginx container to the host Uvicorn process and must not be deployed.
