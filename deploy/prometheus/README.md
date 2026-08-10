# Prometheus deployment assets

Copy `alerts.yml` into the Prometheus rule directory and merge `scrape.yml` into
the site Prometheus configuration. Replace the example target and mount the same
random bearer token configured as `BOOK_SCANNER_METRICS_BEARER_TOKEN` into the
credentials file. Never commit the real token.

Validate before reloading Prometheus:

```bash
promtool check rules /etc/prometheus/rules/book-scanner-alerts.yml
promtool check config /etc/prometheus/prometheus.yml
```

Run Prometheus with a 30-day local retention policy unless the hackerspace's
capacity and compliance requirements specify another reviewed value:

```text
--storage.tsdb.retention.time=30d
--storage.tsdb.retention.size=40GB
```

After deployment, verify `up{job="book-scanner"} == 1`, inspect every rule in the
Prometheus Rules page, and send a test notification through the configured
Alertmanager receiver. The target-down rule is also the ongoing scrape check.
