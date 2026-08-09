# Sprint009 — On-Demand Conversion

## Goal

Provide safe asynchronous conversion between high-value e-book formats and register converted output as new FileVariants.

## Tasks

- Implement generic `Converter` interface.
- Represent supported conversions as a directed graph.
- Select conversion routes based on installed/available tools.
- Implement the highest-value combinations identified in Sprint001.
- Run converters in restricted worker processes where practical.
- Store conversion job state and errors.
- Register generated output as a new FileVariant under the same Book.
- Record full provenance: source variant, converter, version, options, job ID, timestamps.
- Never overwrite existing source variants.
- Expose conversion choices on Book page.
- Avoid promising format pairs that research found unreliable; mark unsupported/deprioritized pairs clearly.

## Tests

- Direct conversion path.
- Multi-hop conversion path.
- Unsupported pair.
- Converter failure.
- Duplicate generated output handling.
- Provenance is preserved.

## Acceptance Criteria

A member can request at least the primary supported conversions from the Book page, leave the request running asynchronously, and later download the newly registered variant.
