# Sprint001 Scanner Protocol Draft (v1)

## Core resources

- `ScanJob`: server-generated UUID, metadata hints, status, operator, scanner id.
- `ScanPage`: logical page identity and one or more immutable artifacts.
- `ScanTodo`: optional tasks like `RESCAN`.

## API surface (v1)

- `POST /api/scan/jobs/` -> create job
- `GET /api/scan/jobs/` -> list open jobs for scanner/operator
- `GET /api/scan/jobs/{job_id}/` -> detail
- `POST /api/scan/jobs/{job_id}/pages/` -> upload page
- `PUT /api/scan/pages/{page_id}/` -> replace/reupload idempotently
- `POST /library/books/{book_id}/ocr/` -> request OCR from library variant
- `POST /scan/jobs/{job_id}/ocr/` -> request OCR from scan pages
- `GET /ocr/{job_id}/` -> OCR job status/provenance
- `POST /api/scan/jobs/{job_id}/finish/` -> complete job

## Page upload contract

- include:
  - logical sequence number
  - optional side (`left`, `right`, `single`)
  - checksum (sha256)
  - capture timestamp
  - scanner metadata blob
- idempotency:
  - repeated upload for same `(job_id, sequence_number, checksum)` returns existing page id

## Storage and resilience

- Scanner sends raw image artifacts; server stores immutable upload.
- Replacements create new page version and mark previous version inactive.
- Source deletion by scanner is decoupled; pipeline policy handles source cleanup.

## Rescan todo flow

- `POST /scan/pages/{page_id}/rescan/` creates a new RESCAN todo for the page.
- `GET /scan/jobs/{job_id}/todos/` lists todos for a specific job.
- `GET /scan/todos/?state=...` lists or filters todos across jobs.
- `POST /scan/todos/{todo_id}/claim/` claims a todo.
- `POST /scan/todos/{todo_id}/complete/` marks a todo completed.
- `POST /scan/todos/{todo_id}/cancel/` cancels a todo.
- `GET /scan/jobs/?with_open_todos=true` lists jobs with open rescan todos.

## Security

- Upload endpoints require authenticated session.
- Filenames are sanitized before storage.
- Parser/extractor boundaries remain server-side behind size and timeout controls.
