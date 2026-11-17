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

## libreflip scanner (TLS 1.3, LAN only)

The scanner device API `/api/scanner/v1/` and the portal are reachable only
inside the c-base LAN, and only over HTTPS with TLS 1.3. The full contract
is
[`libreflip/serif` `docs/sans-serif-interface.md`](https://github.com/libreflip/serif/blob/master/docs/sans-serif-interface.md)
§3. Put a TLS server block in front of the `book-scanner.conf` upstream,
along these lines:

```nginx
server {
    listen 443 ssl;
    server_name serif.c-base.lan;              # the VM's LAN host name
    ssl_certificate     /etc/ssl/serif/serif.crt;  # internal CA or self-signed
    ssl_certificate_key /etc/ssl/serif/serif.key;
    ssl_protocols TLSv1.3;
    allow 10.0.0.0/8;                          # replace with the c-base LAN ranges
    deny all;

    location /api/scanner/ {
        client_max_body_size 64m;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
    # ... the existing locations from book-scanner.conf
}
```

Give the scanner the CA certificate that signed `serif.crt`. It verifies the
chain and host name and never falls back to plain HTTP. Bind Uvicorn to
`127.0.0.1` so the API cannot be reached without TLS.
