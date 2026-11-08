# open-api-spec-model-comparison

Compares the Java models generated from two OpenAPI 3.0 specs and rates every difference by
its wire impact. It works as a Claude Code skill (`/open-api-spec-model-comparison`) and as a
standalone CLI that also runs in CI.

## Requirements
Java 17+, Maven 3.8+, Python 3.9+, Git, and bash (macOS's bash 3.2 works). It needs network
access on the first run: Maven Central for the generator and dependencies, plus your Git host.

## Quick start
```bash
# two local files
scripts/compare.sh --spec1 api/v1.yaml --spec2 api/v2.yaml --baseline 1

# two folders in one repo and branch (a single shallow clone)
scripts/compare.sh --repo1 https://github.com/org/repo.git --ref1 main \
  --spec1 fixtures/decomposed/api/openapi.yaml --spec2 fixtures/monolith/openapi.yaml \
  --label1 decomposed --label2 monolith --baseline 2

# CI gate: fail the build on breaking changes between main and the PR branch
scripts/compare.sh --repo1 "$REPO" --ref1 main --repo2 "$REPO" --ref2 "$BRANCH" \
  --spec1 api/openapi.yaml --spec2 api/openapi.yaml --baseline 1 --fail-on-breaking
```
Outputs land in `--out` (default `./openapi-comparison`):

| Path | Content |
|---|---|
| `reports/equivalence-report.html` | Verdict, class-by-class table, every difference with its impact |
| `reports/summary.json` | The same data as JSON, for CI or other tooling |
| `model-diff.json` | Raw reflection diff, facts only and unrated |
| `validation/<label>-cli.txt` | openapi-generator validator output |
| `normalized/<label>/openapi.json` | Each spec resolved into a single file |
| `logs/` | Full logs for every step |

Exit codes: `0` finished · `1` breaking differences found with `--fail-on-breaking` · `2` usage error · `3` tool failure.

## How it works
1. **Fetch.** Takes local paths, or shallow-clones Git sources. One clone is shared when both specs live in the same repo and ref.
2. **Validate.** `openapi-generator-cli validate --recommend` for each spec. The jar is cached in `~/.cache/openapi-model-comparison`.
3. **Normalize.** Resolves each spec into a single JSON file, used for spec-level differences like operations, parameters, responses and version.
4. **Generate and compile.** `assets/pom.xml` generates both model sets with identical, pinned settings (`spring`, models only) into
   `com.compare.spec1.model` and `com.compare.spec2.model`, and compiles them. A compile failure is itself a finding.
5. **Compare.** `assets/ModelComparator.java` loads both packages by reflection. It compares JSON names, Java types, required,
   nullable, readOnly, Bean Validation constraints, defaults, descriptions and examples, and pairs renamed classes and properties by structure.
6. **Rate and render.** `scripts/render_report.py` gives every difference an impact from a fixed rule table, so reruns are
   reproducible, then writes the HTML and JSON.

## Impact rules
| Difference | Impact |
|---|---|
| JSON property renamed, wire type changed (e.g. `date` vs `date-time`), enum value on one side only, default changed | breaking |
| Required on one side only (with `--baseline`: became required) | breaking (became optional: risky) |
| Required property, or schema used directly by an endpoint, on one side only | breaking |
| Optional property on one side only | risky (with `--baseline`: removed is breaking, added is risky) |
| Numeric bound or length differs | breaking (with `--baseline`: loosened is risky) |
| Constraint present on one side only, nullable or readOnly differs | risky |
| Class renamed but same JSON, integer width differs (`Integer` vs `Long`) | safe |
| Schema referenced by nothing (e.g. a leftover `allOf` base) | cosmetic |
| Description or example differs | cosmetic |
| Endpoint, or required parameter, on one side only, parameter renamed or retyped, request body changed | breaking |
| Optional parameter on one side only, response status on one side only | risky |
| `operationId` differs | safe |

## Overrides
The rules can't know how a field is used. To re-rate or annotate a finding, pass `--overrides overrides.json`:
```json
[
  {"match": {"cls": "Doctor", "field": "yearsOfExperience", "aspect": "constraint.minimum"},
   "impact": "cosmetic", "note": "Response-only field; server never sends negatives."},
  {"match": {"where": "Patient.insurance"}, "impact": "breaking",
   "note": "Insurance data is required for billing; dropping it loses data."}
]
```
Match keys are any finding fields from `summary.json` (`where`, `what`, `cls`, `field`, `aspect`, `scope`, `category`).
Overridden findings are marked in the report.

## Validation reports
`scripts/render_validation.py` combines the validator output with review findings written as JSON. The format is in the
script's docstring. In the skill, Claude writes the findings and you decide which fixes to apply.

## Testing
```bash
tests/selftest.sh
```
This runs a zero-diff control (a spec against itself, which must give 0 findings) and a planted case (`tests/fixtures/base.yaml` vs
`changed.yaml`) with 18 known differences across renames, types, enums, required, nullable, constraints, defaults,
missing properties, an unreferenced `allOf` base, a parameter rename and a missing `operationId`. Each one must be found with the
expected impact. It takes about 15 s with a warm Maven cache. Run it after changing anything in `scripts/` or `assets/`.

## Known limits
- Only the `spring` generator's annotations are read. Other generators need `ModelComparator` adjusted.
- Inline schemas get generator-chosen names (e.g. `CancelAppointmentRequest`). They pair by name only when both specs inline them the same way.
- `allOf` without a discriminator is flattened, so composition differences show up only as an unreferenced base class.
- `oneOf`/`anyOf` and discriminator mappings are compared only as far as they shape the generated classes.
