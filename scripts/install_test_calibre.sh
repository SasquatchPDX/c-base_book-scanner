#!/usr/bin/env bash
set -Eeuo pipefail

book_scanner_calibre_destination="${1:?destination directory required}"
book_scanner_calibre_version="9.11.0"
book_scanner_calibre_architecture="$(uname -m)"

case "${book_scanner_calibre_architecture}" in
  x86_64)
    book_scanner_calibre_asset_arch="x86_64"
    book_scanner_calibre_sha512="4b2250124e73b907dc84f30d413e095193735ffe3f933793a7d021885efbb37b2a92254e36c17e0a52c555729a7cd67c5229a1b62b9968baf61764463aeea47e"
    ;;
  aarch64 | arm64)
    book_scanner_calibre_asset_arch="arm64"
    book_scanner_calibre_sha512="9c64ecc70a11244df268137c1c1c48c969d53bd8416aed1a8e59f3ac5add779803e64ed55649c2f6374acf59526f7caa7b325b8e9ff92f0e7aee43a0f301357d"
    ;;
  *)
    echo "unsupported Calibre test architecture: ${book_scanner_calibre_architecture}" >&2
    exit 1
    ;;
esac

book_scanner_calibre_archive="${book_scanner_calibre_destination}/calibre.txz"
book_scanner_calibre_install_root="${book_scanner_calibre_destination}/calibre"
book_scanner_calibre_url="https://download.calibre-ebook.com/${book_scanner_calibre_version}/calibre-${book_scanner_calibre_version}-${book_scanner_calibre_asset_arch}.txz"

mkdir -p "${book_scanner_calibre_install_root}"
curl --fail --location --retry 3 --proto '=https' --tlsv1.2 \
  --output "${book_scanner_calibre_archive}" "${book_scanner_calibre_url}"
printf '%s  %s\n' "${book_scanner_calibre_sha512}" "${book_scanner_calibre_archive}" \
  | sha512sum --check --strict
tar --extract --xz --file "${book_scanner_calibre_archive}" \
  --directory "${book_scanner_calibre_install_root}"
rm "${book_scanner_calibre_archive}"
"${book_scanner_calibre_install_root}/ebook-convert" --version
