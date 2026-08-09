# Sprint008 — Search, Browse, and Digital Library V1

## Goal

Turn the ingestion backend into a genuinely usable shared library.

## Tasks

- Implement search over canonical Book metadata.
- Browse/filter by title, author, category, genre, language, and available format.
- Implement Book detail page.
- Show front and back covers when available.
- Show bibliographic metadata and description.
- Show available FileVariants and file sizes/formats.
- Implement authorized downloads using storage streaming rather than exposing raw network paths.
- Add pagination and sensible sorting.
- Add basic library landing/recent additions page.
- Ensure normal users cannot delete.
- Add admin delete workflows with explicit confirmation and storage/database consistency checks.

## E2E Test

`LDAP login -> upload/import -> metadata identification -> optional review -> book appears in search -> book page -> download`

## Acceptance Criteria

Members can use the application as a functional shared e-book library without scanner or ScanTailor features.

This sprint defines the first major usable release baseline.
