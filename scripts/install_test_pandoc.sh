#!/usr/bin/env bash
set -Eeuo pipefail

book_scanner_pandoc_destination="${1:?destination directory required}"
book_scanner_pandoc_version="3.9.0.2"
book_scanner_pandoc_architecture="$(uname -m)"

case "${book_scanner_pandoc_architecture}" in
  x86_64)
    book_scanner_pandoc_asset_arch="amd64"
    book_scanner_pandoc_sha256="a69abfababda8a56969a254b09f9553a7be89ddec00d4e0fe9fd585d71a67508"
    ;;
  aarch64 | arm64)
    book_scanner_pandoc_asset_arch="arm64"
    book_scanner_pandoc_sha256="b6d21e8f9c3b15744f5a7ab40248019157ed7793875dbe0383d4c82ff572b528"
    ;;
  *)
    echo "unsupported Pandoc test architecture: ${book_scanner_pandoc_architecture}" >&2
    exit 1
    ;;
esac

book_scanner_pandoc_archive="${book_scanner_pandoc_destination}/pandoc.tar.gz"
book_scanner_pandoc_url="https://github.com/jgm/pandoc/releases/download/${book_scanner_pandoc_version}/pandoc-${book_scanner_pandoc_version}-linux-${book_scanner_pandoc_asset_arch}.tar.gz"

mkdir -p "${book_scanner_pandoc_destination}"
curl --fail --location --retry 3 --proto '=https' --tlsv1.2 \
  --output "${book_scanner_pandoc_archive}" "${book_scanner_pandoc_url}"
printf '%s  %s\n' "${book_scanner_pandoc_sha256}" "${book_scanner_pandoc_archive}" \
  | sha256sum --check --strict
tar --extract --gzip --file "${book_scanner_pandoc_archive}" \
  --directory "${book_scanner_pandoc_destination}" --strip-components=1
rm "${book_scanner_pandoc_archive}"
"${book_scanner_pandoc_destination}/bin/pandoc" --version
