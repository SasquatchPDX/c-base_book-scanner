# Sprint014 — Page Split and Deskew

## Goal

Implement the first two functional ScanTailor-Web image-processing stages with automatic and manual controls.

## Page Split

Support:

- single-page image
- facing-page image
- automatic split detection
- manual split adjustment
- batch/default application where appropriate

## Deskew

Support:

- automatic skew-angle detection
- manual angle correction
- reset
- batch apply where appropriate
- fast preview and full-resolution worker processing

## Engineering Requirements

- Algorithms live behind modular interfaces.
- Store algorithm names/versions and parameters.
- Preserve input artifacts.
- Use stage dependency invalidation.
- Do not encode UI state as the only source of processing truth.

## Tests

- Representative synthetic and real-ish fixtures.
- Automatic split candidate behavior.
- Manual split override.
- Deskew parameter reproducibility.
- Upstream split change invalidates deskew and later stages.

## Acceptance Criteria

An operator can take raw page/facing-page photographs through Page Split and Deskew, make manual corrections in the browser, and regenerate stable full-resolution outputs.
