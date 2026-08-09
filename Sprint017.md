# Sprint017 — Output and Binarization

## Goal

Implement final cleaned-page generation including multiple adaptive thresholding algorithms and text/picture handling.

## Tasks

Implement pluggable output algorithms including, as practical based on research:

- Otsu
- Sauvola
- Wolf
- Fox
- Window
- Grad

Add:

- grayscale/color output where appropriate
- text/picture zone separation
- manual zone controls
- output preview
- batch defaults with per-page overrides
- final cleaned-page artifact generation
- algorithm/version/parameter provenance

Avoid writing bespoke implementations when high-quality compatible libraries already exist, unless the project specifically requires algorithm reproduction.

## Tests

- Fixed image fixtures for each supported algorithm.
- Regression checks against known outputs/tolerances.
- Zone handling.
- Parameter persistence.
- Switching algorithms invalidates only appropriate downstream outputs.

## Acceptance Criteria

An operator can generate consistent cleaned page images from the earlier ScanTailor stages and choose appropriate output/binarization behavior for text and mixed-content pages.
