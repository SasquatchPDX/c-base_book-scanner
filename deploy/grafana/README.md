# Grafana deployment assets

Create a Prometheus data source with UID `prometheus`, mount
`book-scanner-dashboard.json` below `/var/lib/grafana/dashboards/book-scanner/`,
and mount the provisioning file in Grafana's dashboard-provisioning directory.
The dashboard uses only bounded application metric labels and contains no book
titles, filenames, usernames, or contents.
