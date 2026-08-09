# Sprint001 Conversion Research (Initial Matrix)

The matrix below is intentionally explicit about confidence and implementation status. Items marked “not implemented” are planned tasks.

| Source | Target | Preferred tool path | Fallback | Quality expectation | Lossy? | OCR required | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| EPUB | MOBI | Calibre `ebook-convert` | `calibre` older path | good for common text EPUBs | yes | no | research-only |
| MOBI | EPUB | Calibre `ebook-convert` | none | good | yes | no | research-only |
| EPUB | PDF | Calibre / Pandoc | `ebook-convert` + layout options | good for readable output, variable layout | yes | no | research-only |
| PDF | EPUB | Calibre | `ebook-convert` + OCR-prep if scanned PDF | mixed (scanned PDFs degrade) | yes | optional if scanned | research-only |
| HTML | EPUB | Calibre / Pandoc | BeautifulSoup + ebooklib pipeline | good for structured HTML | yes | no | research-only |
| TXT | EPUB | Calibre / Pandoc | ebooklib wrapper | fair (formatting limited) | yes | no | research-only |
| FB2 | EPUB | Calibre / ebook-convert | ebooklib custom extraction | good | yes | no | research-only |
| DjVu -> EPUB/PDF | DjVuLibre text extraction then conversion | Calibre if supported in deployment | manual fallback | uncertain | yes | maybe | research-only |
| CBZ/CBR extraction | Unarchive via libarchive/7zip | python-libarchive-c / zipfile/rarfile | n/a | good if source clean | no | no | research-only |
| scanned images -> PDF | img2pdf | Ghostscript + pdftk alternatives | pillow/pdfkit | good for archival | yes | no | research-only |
| OCR text -> EPUB/PDF | post-process pipeline into ebooklib/Calibre | custom assembly scripts | good | yes | no | research-only |

## Notes

- This matrix is a starting point and will be updated in Sprint009 when actual tool availability is validated in the target environment.
- The implementation should treat converter availability as runtime-detected and expose unavailable conversions as disabled in UI.
