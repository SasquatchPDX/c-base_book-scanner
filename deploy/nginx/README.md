# Nginx reverse proxy

`book-scanner.conf` is a production-oriented HTTP upstream example for an HTTPS
terminator or ingress. Point `book-scanner-web:8000` at the Uvicorn service and
keep the dedicated ScanTailor event-stream location unbuffered. Its 75-second
timeouts exceed the default 45-second application stream lifetime.

The application trusts forwarded scheme information only when
`BOOK_SCANNER_SECURE_PROXY_SSL_HEADER=true`. Restrict direct access to Uvicorn so
clients cannot forge trusted proxy headers. Configure TLS, certificates, host
names, request-size policy, and static-file handling to match the site.

`integration.conf` is only for the Docker-backed acceptance suite. It connects a
pinned Nginx container to the host Uvicorn process and must not be deployed.
