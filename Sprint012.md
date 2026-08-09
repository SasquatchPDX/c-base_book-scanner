# Sprint012 — OCR Service Integration

## Goal

Offload OCR work to the hackerspace AI server and register OCR-derived artifacts in the library pipeline.

## Tasks

- Define/document OCR client API contract.
- Support asynchronous submission and status polling/callback strategy appropriate to the AI server.
- Submit cleaned page batches and image-only PDFs where supported.
- Retrieve OCR results.
- Store OCR job provenance:
  - source variant/artifact
  - engine/model
  - model/version
  - language/settings
  - timestamps
  - confidence metadata if available
- Support initial useful outputs such as plain/structured text and searchable PDF.
- Make output eligible for later EPUB/text conversion.
- Surface failures and retry controls.
- Do not perform substantial OCR compute on the e-book VM.

## Tests

Use a test-double OCR service for:

- accepted job
- running job
- successful result
- failed job
- timeout/retry
- malformed response

## Acceptance Criteria

A clean scan can be submitted to the external AI server, processed asynchronously, and returned as an OCR-derived variant/artifact with traceable provenance.
