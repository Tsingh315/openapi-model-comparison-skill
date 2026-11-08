#!/usr/bin/env bash
# Compare the Java models generated from two OpenAPI 3.0 specs.
#
# Pipeline: fetch sources -> validate -> normalize -> generate + compile models
#           -> reflect-compare classes -> render HTML report + summary JSON.
#
# Run with --help for usage. Exit codes:
#   0  finished (and, with --fail-on-breaking, no breaking differences)
#   1  --fail-on-breaking was set and breaking differences were found
#   2  usage error
#   3  a tool or build step failed (see the log path printed)
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SKILL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/openapi-model-comparison"

GEN_VERSION="7.10.0"
OUT_DIR="./openapi-comparison"
SPEC1="" SPEC2="" REPO1="" REPO2="" REF1="" REF2=""
LABEL1="spec1" LABEL2="spec2"
BASELINE=""
OVERRIDES=""
SKIP_VALIDATION=0
FAIL_ON_BREAKING=0

usage() {
  cat <<'EOF'
Usage: compare.sh --spec1 PATH --spec2 PATH [options]

Spec locations
  --spec1 PATH, --spec2 PATH   OpenAPI file. A local path, or a path relative to --repoN.
  --repo1 SRC,  --repo2 SRC    Optional. Git URL or local directory holding the spec.
                               If --repo2 is omitted it defaults to --repo1.
  --ref1 REF,   --ref2 REF     Branch or tag to check out (default: the remote's default branch).
                               If --ref2 is omitted it defaults to --ref1.

Report
  --label1 NAME, --label2 NAME Display names in the report (default: spec1, spec2).
  --baseline 1|2               Treat that spec as the older contract when rating impact.
                               If omitted, impact is rated in both directions.
  --overrides FILE             JSON file that re-rates or annotates specific findings
                               (see README.md, "Overrides").
  --out DIR                    Working and output directory (default: ./openapi-comparison).

Behaviour
  --generator-version VER      openapi-generator version (default: 7.10.0).
  --skip-validation            Don't run the validator first.
  --fail-on-breaking           Exit 1 if any breaking difference is found (for CI).
  -h, --help                   Show this help.

Outputs (under --out)
  reports/equivalence-report.html   human-readable report
  reports/summary.json              machine-readable counts and findings
  model-diff.json                   raw class-by-class diff
  validation/<label>-cli.txt        validator output
  normalized/<label>/openapi.json   single-file resolved spec
  logs/                             full tool logs
EOF
}

log()  { printf '\033[1m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die()  { printf 'error: %s\n' "$1" >&2; exit "${2:-3}"; }
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not on PATH" 3; }

on_error() { die "step failed at line $1. Logs: ${OUT_DIR}/logs/" 3; }
trap 'on_error $LINENO' ERR

# ------------------------------------------------------------------ args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --spec1) SPEC1="${2:?}"; shift 2 ;;
    --spec2) SPEC2="${2:?}"; shift 2 ;;
    --repo1) REPO1="${2:?}"; shift 2 ;;
    --repo2) REPO2="${2:?}"; shift 2 ;;
    --ref1)  REF1="${2:?}";  shift 2 ;;
    --ref2)  REF2="${2:?}";  shift 2 ;;
    --label1) LABEL1="${2:?}"; shift 2 ;;
    --label2) LABEL2="${2:?}"; shift 2 ;;
    --baseline) BASELINE="${2:?}"; shift 2 ;;
    --overrides) OVERRIDES="${2:?}"; shift 2 ;;
    --out) OUT_DIR="${2:?}"; shift 2 ;;
    --generator-version) GEN_VERSION="${2:?}"; shift 2 ;;
    --skip-validation) SKIP_VALIDATION=1; shift ;;
    --fail-on-breaking) FAIL_ON_BREAKING=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" 2 ;;
  esac
done

[[ -n "$SPEC1" && -n "$SPEC2" ]] || { usage >&2; die "--spec1 and --spec2 are required" 2; }
[[ -z "$BASELINE" || "$BASELINE" == 1 || "$BASELINE" == 2 ]] || die "--baseline must be 1 or 2" 2
[[ -z "$OVERRIDES" || -f "$OVERRIDES" ]] || die "overrides file not found: $OVERRIDES" 2
[[ "$LABEL1" != "$LABEL2" ]] || die "--label1 and --label2 must differ" 2
[[ -z "$REPO2" ]] && REPO2="$REPO1"
[[ -z "$REF2" ]] && REF2="$REF1"

need java; need mvn; need python3; need git

mkdir -p "$OUT_DIR"/{logs,validation,normalized,reports,sources} "$CACHE_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
[[ -z "$OVERRIDES" ]] || OVERRIDES="$(cd "$(dirname "$OVERRIDES")" && pwd)/$(basename "$OVERRIDES")"

# ------------------------------------------------------------------ 1. sources
# fetch_source <repo> <ref> <dest>: prints the directory holding the checkout.
fetch_source() {
  local repo="$1" ref="$2" dest="$3"
  if [[ -z "$repo" ]]; then pwd; return; fi
  if [[ -d "$repo" ]]; then (cd "$repo" && pwd); return; fi     # local directory: use in place
  if [[ -d "$dest/.git" ]] && [[ "$(git -C "$dest" remote get-url origin)" == "$repo" ]]; then
    git -C "$dest" fetch -q --depth 1 origin "${ref:-HEAD}" >>"$OUT_DIR/logs/git.log" 2>&1
    git -C "$dest" checkout -q --detach FETCH_HEAD >>"$OUT_DIR/logs/git.log" 2>&1
  else
    rm -rf "$dest"
    git clone -q --depth 1 ${ref:+--branch "$ref"} "$repo" "$dest" >>"$OUT_DIR/logs/git.log" 2>&1
  fi
  echo "$dest"
}

log "[1/6] Fetching sources"
ROOT1="$(fetch_source "$REPO1" "$REF1" "$OUT_DIR/sources/$LABEL1")"
if [[ "$REPO2" == "$REPO1" && "$REF2" == "$REF1" ]]; then
  ROOT2="$ROOT1"   # same repo and ref: one checkout serves both specs
else
  ROOT2="$(fetch_source "$REPO2" "$REF2" "$OUT_DIR/sources/$LABEL2")"
fi
resolve() { [[ "$2" = /* ]] && echo "$2" || echo "$1/$2"; }
SPEC1_ABS="$(resolve "$ROOT1" "$SPEC1")"; SPEC2_ABS="$(resolve "$ROOT2" "$SPEC2")"
[[ -f "$SPEC1_ABS" ]] || die "spec1 not found: $SPEC1_ABS" 2
[[ -f "$SPEC2_ABS" ]] || die "spec2 not found: $SPEC2_ABS" 2
commit_of() { git -C "$(dirname "$1")" rev-parse --short HEAD 2>/dev/null || echo "n/a"; }
COMMIT1="$(commit_of "$SPEC1_ABS")"; COMMIT2="$(commit_of "$SPEC2_ABS")"

# ------------------------------------------------------------------ 2. generator CLI (cached)
CLI_JAR="$CACHE_DIR/openapi-generator-cli-$GEN_VERSION.jar"
if [[ ! -f "$CLI_JAR" ]]; then
  log "      Downloading openapi-generator-cli $GEN_VERSION (one time, cached in $CACHE_DIR)"
  mvn -q -B dependency:copy -Dartifact="org.openapitools:openapi-generator-cli:$GEN_VERSION" \
      -DoutputDirectory="$CACHE_DIR" >"$OUT_DIR/logs/cli-download.log" 2>&1
fi

# ------------------------------------------------------------------ 3. validate
if [[ "$SKIP_VALIDATION" -eq 0 ]]; then
  log "[2/6] Validating both specs"
  invalid=0
  for side in 1 2; do
    label_var="LABEL$side"; spec_var="SPEC${side}_ABS"
    out="$OUT_DIR/validation/${!label_var}-cli.txt"
    if java -jar "$CLI_JAR" validate -i "${!spec_var}" --recommend >"$out" 2>&1; then
      log "      ${!label_var}: $(tail -n 1 "$out")"
    else
      log "      ${!label_var}: INVALID (see $out)"; invalid=1
    fi
  done
  [[ "$invalid" -eq 0 ]] || die "at least one spec is structurally invalid; fix it before comparing" 3
else
  log "[2/6] Validation skipped"
fi

# ------------------------------------------------------------------ 4. normalize
log "[3/6] Normalizing specs (resolving \$refs into one file each)"
for side in 1 2; do
  label_var="LABEL$side"; spec_var="SPEC${side}_ABS"
  rm -rf "$OUT_DIR/normalized/${!label_var}"
  java -jar "$CLI_JAR" generate -g openapi -i "${!spec_var}" -o "$OUT_DIR/normalized/${!label_var}" \
       >"$OUT_DIR/logs/normalize-${!label_var}.log" 2>&1
done

# ------------------------------------------------------------------ 5. generate + compile models
log "[4/6] Generating and compiling models"
BUILD_DIR="$OUT_DIR/build"
mkdir -p "$BUILD_DIR"
cp "$SKILL_DIR/assets/pom.xml" "$BUILD_DIR/pom.xml"
rm -rf "$BUILD_DIR/target"
MVN_ARGS=(-B -f "$BUILD_DIR/pom.xml" -Dspec1="$SPEC1_ABS" -Dspec2="$SPEC2_ABS" -Dopenapi-generator.version="$GEN_VERSION")
if ! mvn "${MVN_ARGS[@]}" compile >"$OUT_DIR/logs/build.log" 2>&1; then
  grep -E '^\[ERROR\]' "$OUT_DIR/logs/build.log" | head -n 20 >&2 || true
  die "model generation or compilation failed. Full log: $OUT_DIR/logs/build.log" 3
fi
mvn -q "${MVN_ARGS[@]}" dependency:build-classpath -Dmdep.outputFile="$BUILD_DIR/target/classpath.txt" \
    >"$OUT_DIR/logs/classpath.log" 2>&1

# ------------------------------------------------------------------ 6. compare
log "[5/6] Comparing generated classes"
CP="$BUILD_DIR/target/classes:$(cat "$BUILD_DIR/target/classpath.txt")"
java -cp "$CP" "$SKILL_DIR/assets/ModelComparator.java" "$BUILD_DIR/target/classes" \
     com.compare.spec1.model com.compare.spec2.model >"$OUT_DIR/model-diff.json" 2>"$OUT_DIR/logs/compare.log"

# ------------------------------------------------------------------ 7. report
log "[6/6] Rendering report"
REPORT_ARGS=(
  --diff "$OUT_DIR/model-diff.json"
  --spec-json1 "$OUT_DIR/normalized/$LABEL1/openapi.json"
  --spec-json2 "$OUT_DIR/normalized/$LABEL2/openapi.json"
  --label1 "$LABEL1" --label2 "$LABEL2"
  --source1 "${REPO1:+$REPO1 @ ${REF1:-default} ($COMMIT1) : }$SPEC1"
  --source2 "${REPO2:+$REPO2 @ ${REF2:-default} ($COMMIT2) : }$SPEC2"
  --generator-version "$GEN_VERSION"
  --out "$OUT_DIR/reports/equivalence-report.html"
  --summary "$OUT_DIR/reports/summary.json"
)
[[ -z "$BASELINE" ]]  || REPORT_ARGS+=(--baseline "$BASELINE")
[[ -z "$OVERRIDES" ]] || REPORT_ARGS+=(--overrides "$OVERRIDES")
[[ "$FAIL_ON_BREAKING" -eq 0 ]] || REPORT_ARGS+=(--fail-on-breaking)

trap - ERR
set +e
python3 -I "$SCRIPT_DIR/render_report.py" "${REPORT_ARGS[@]}"
rc=$?
set -e
[[ $rc -le 1 ]] || die "report rendering failed" 3
log "Report: $OUT_DIR/reports/equivalence-report.html"
exit "$rc"
