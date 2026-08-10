# Sprint001 Conversion Research (Initial Matrix)

The matrix below distinguishes runtime implementation from binary-backed
acceptance evidence. Converter routes are advertised only when their executable
is present on the worker.

| Source | Target | Preferred tool path | Fallback | Quality expectation | Lossy? | OCR required | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| EPUB | MOBI | Calibre `ebook-convert` | `calibre` older path | good for common text EPUBs | yes | no | implemented; Calibre binary acceptance pending |
| MOBI | EPUB | Calibre `ebook-convert` | none | good | yes | no | implemented; Calibre binary acceptance pending |
| EPUB | PDF | Calibre | `ebook-convert` + layout options | good for readable output, variable layout | yes | no | implemented; Calibre binary acceptance pending |
| PDF | EPUB | Calibre | `ebook-convert` + OCR-prep if scanned PDF | mixed (scanned PDFs degrade) | yes | optional if scanned | implemented; Calibre binary acceptance pending |
| HTML | EPUB | Pandoc | Calibre | good for structured HTML | yes | no | implemented; HTML fixture acceptance pending |
| TXT | EPUB | Pandoc | Calibre | fair (formatting limited) | yes | no | implemented; Pandoc 3.9.0.2 binary-verified in CI |
| FB2 | EPUB | Calibre `ebook-convert` | none | good | yes | no | researched; not advertised |
| DjVu -> EPUB/PDF | DjVuLibre text extraction then conversion | Calibre if supported in deployment | manual fallback | uncertain | yes | maybe | researched; not advertised |
| CBZ/CBR extraction | Standard ZIP / RarFile and configured archive tool | libarchive/7zip | n/a | good if source clean | no | no | bounded intake implemented; conversion not advertised |
| scanned images -> PDF | img2pdf | Ghostscript alternatives | Pillow | good for archival | yes | no | scan packaging implemented and library-backed |
| OCR text -> EPUB | OCR text then Pandoc | Calibre | good | yes | no | orchestration implemented; full real-service flow pending |

## Notes

- Pandoc 3.9.0.2 test archives are fetched from the upstream release and verified
  against the publisher's SHA-256 before execution on amd64 or arm64.
- Calibre remains optional and runtime-detected; its higher-risk PDF/MOBI routes
  are not yet supported by a pinned binary acceptance environment.
