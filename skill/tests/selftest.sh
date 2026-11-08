#!/usr/bin/env bash
# Self-test for the comparison pipeline. Run it after changing any script, rule or pinned version.
#
#   1. Zero-diff control: base.yaml against itself must give 0 findings and exit 0.
#   2. Planted case: base.yaml against changed.yaml must find every planted difference
#      with the expected impact, and --fail-on-breaking must exit 1.
#
# Usage: tests/selftest.sh [work-dir]     (default: a fresh temp dir, removed on success)
set -Eeuo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SKILL_DIR="$(cd "$HERE/.." && pwd)"
WORK="${1:-$(mktemp -d "${TMPDIR:-/tmp}/oamc-selftest.XXXXXX")}"
FIX="$HERE/fixtures"
fail() { printf 'SELFTEST FAIL: %s\n' "$*" >&2; exit 1; }

echo "== 1/2 zero-diff control"
"$SKILL_DIR/scripts/compare.sh" --spec1 "$FIX/base.yaml" --spec2 "$FIX/base.yaml" \
    --label1 a --label2 b --out "$WORK/control" --fail-on-breaking \
  || fail "control run exited $? (expected 0)"
python3 -I - "$WORK/control/reports/summary.json" <<'PY' || fail "control produced findings"
import json, sys
s = json.load(open(sys.argv[1]))
assert not s["findings"], s["findings"]
assert s["counts"]["different"] == 0 and s["counts"]["only"] == 0, s["counts"]
print("   ok: 0 findings,", s["counts"]["identical"], "identical classes")
PY

echo "== 2/2 planted differences"
set +e
"$SKILL_DIR/scripts/compare.sh" --spec1 "$FIX/base.yaml" --spec2 "$FIX/changed.yaml" \
    --label1 base --label2 changed --baseline 1 --out "$WORK/planted" --fail-on-breaking
rc=$?
set -e
[[ $rc -eq 1 ]] || fail "planted run exited $rc (expected 1: breaking differences found)"

python3 -I - "$WORK/planted/reports/summary.json" <<'PY' || fail "planted differences not reported as expected"
import json, sys
s = json.load(open(sys.argv[1]))
got = {(f["where"], f["what"], f["impact"]) for f in s["findings"]}
expected = {
    ("Customer", "Schema only in base", "risky"),
    ("Base", "Schema only in changed", "cosmetic"),
    ("DeliveryWindow / TimeWindow", "Class name", "safe"),
    ("OrderStatus", "Enum value only in base", "breaking"),
    ("OrderStatus", "Enum value only in changed", "breaking"),
    ("Order.category → section", "JSON property name", "breaking"),
    ("Order.quantity", "Java type", "safe"),
    ("Order.placedOn", "Java type", "breaking"),
    ("Order.email", "Required", "breaking"),
    ("Order.email", "Description", "cosmetic"),
    ("Order.note", "Nullable", "risky"),
    ("Order.note", "maxLength", "breaking"),
    ("Order.giftWrap", "Default", "breaking"),
    ("Order.customer", "Property only in base", "breaking"),
    ("Order.trackingUrl", "Property only in changed", "risky"),
    ("info.version", "API version", "cosmetic"),
    ("GET /orders (query parameter)", "Parameter renamed", "breaking"),
    ("GET /orders/{orderId}/window", "operationId", "safe"),
}
missing, extra = expected - got, got - expected
for m in sorted(missing): print("   MISSING:", m)
for x in sorted(extra):   print("   UNEXPECTED:", x)
assert not missing and not extra
print(f"   ok: all {len(expected)} planted differences found with the expected impact")
PY

[[ $# -ge 1 ]] || rm -rf "$WORK"
echo "SELFTEST PASS"
