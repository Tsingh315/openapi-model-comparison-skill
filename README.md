# openapi-model-comparison-skill

Developing and testing an agent skill that compares the data models defined by two
OpenAPI 3.0 specs.

## Layout
- `skill/SKILL.md` — the skill, as a working draft to iterate on.
- `fixtures/monolith/openapi.yaml` — a single-file OpenAPI spec for a clinic
  booking API (patients, doctors, appointments).
- `fixtures/decomposed/` — the same API, split into `openapi.yaml` + `paths/`,
  `schemas/`, `components/` via `$ref`, with a number of intentional mismatches
  against the monolith (property renames, type changes, dropped constraints,
  enum drift, structural traps) to exercise the skill against.

Run the skill with `fixtures/monolith/openapi.yaml` as side A and
`fixtures/decomposed/api/openapi.yaml` as side B, then check its report against
what was actually planted. The answer key for the fixtures is kept outside
this repo on purpose, so the skill can't read it.
