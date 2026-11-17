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

Cover thumbnails are served by nginx too. Set
`BOOK_SCANNER_COVER_THUMBNAIL_ROOT=/var/lib/book-scanner/cover-thumbnails` and
`BOOK_SCANNER_COVER_THUMBNAIL_ACCEL_PREFIX=/_protected/cover-thumbnails/`; the
`internal` location in `book-scanner.conf` must alias the same directory, and nginx
needs read access to it. Django still checks the session and the member's status for
every thumbnail, then hands the file to nginx with `X-Accel-Redirect`. Thumbnail URLs
contain the file's SHA-256, so the `immutable` cache header is safe. Without the
prefix Django streams the files itself, which is what development and the integration
suite do.

`integration.conf` is only for the Docker-backed acceptance suite. It connects a
pinned Nginx container to the host Uvicorn process and must not be deployed.

## libreflip scanner: TLS 1.3, LAN only (`serif-lan-tls.conf`)

The scanner device API `/api/scanner/v1/` must only be reachable inside the
LAN and only over TLS 1.3 (contract: libreflip/serif
`docs/sans-serif-interface.md` §3, currently
[libreflip/serif#37](https://github.com/libreflip/serif/pull/37)).
`serif-lan-tls.conf` is the front end for that: a `listen 443 ssl` server with
`ssl_protocols TLSv1.3`, LAN `allow`/`deny`, `server_tokens off`, a 150m
server-level `client_max_body_size` and 64m for `/api/scanner/`, plus a
`listen 80` server that redirects browsers to HTTPS and refuses the API.

- Install it next to `book-scanner.conf`. It reuses the `book_scanner_asgi`
  upstream (point that at `127.0.0.1:8000`) with the same
  `proxy_http_version 1.1` and forwarded headers, and carries over the
  unbuffered ScanTailor event-stream location.
- **It fails closed.** As shipped, both servers contain only `deny all;` and
  commented example `allow` lines (RFC 5737 documentation ranges), so every
  client gets `403` until the real LAN ranges are added above `deny all;`.
- Replace the `REPLACE` placeholders: the LAN host name (must match the
  certificate) and the certificate paths.
- `book-scanner.conf`'s plain-HTTP `listen 8080` server answers `403` on
  `/api/scanner/`, so a device key never travels over plain HTTP through it.
  On the VM, bind it to `127.0.0.1:8080` or delete that server block, so the
  portal is only reachable through the TLS server.
- Set `BOOK_SCANNER_SECURE_PROXY_SSL_HEADER=true`. Without it Django sees
  every proxied request as HTTP and the production HTTPS redirect loops.
- Run Uvicorn on `127.0.0.1` only (`deploy/systemd/book-scanner-web.service`
  already does).
- The port-80 redirect uses `try_files` into a named location instead of a
  bare `return 301`, because `return` runs before `allow`/`deny` and would
  answer hosts outside the LAN.

Certificates, NTP and scanner onboarding are in `docs/operations.md`
("libreflip scanner deployment"). CI runs `nginx -t` on both files together,
and `scripts/test_scanner_tls_proxy.sh` (part of the infrastructure suite)
checks a live nginx: TLS 1.2-only clients cannot connect, sources outside the
allowed ranges and the unedited file get `403`, `/api/scanner/` rejects bodies
above 64 MiB with `413`, and neither plain-HTTP listener serves the API.
