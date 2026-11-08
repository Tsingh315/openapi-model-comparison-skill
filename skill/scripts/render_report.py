#!/usr/bin/env python3
"""Rate and render the model comparison as an HTML report plus a summary JSON.

Inputs:
  --diff          model-diff.json written by ModelComparator.java (facts only)
  --spec-json1/2  normalized single-file specs (openapi-generator -g openapi), used for
                  spec-level differences (operations, parameters, responses, version)
                  and to tell whether a schema is used directly by an endpoint

Every finding gets an impact from a fixed rule table (IMPACT_RULES below) so two
runs over the same input always agree. Use --overrides to re-rate or annotate a
specific finding when the rule is wrong for your API; the override is shown in
the report.

Standard library only. Exit codes: 0 ok, 1 breaking found with --fail-on-breaking,
2 bad input.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

BREAKING, RISKY, SAFE, COSMETIC = "breaking", "risky", "safe", "cosmetic"
IMPACT_ORDER = {BREAKING: 0, RISKY: 1, SAFE: 2, COSMETIC: 3}
INTEGER_TYPES = {"Integer", "Long", "Short", "Byte", "BigInteger"}
DECIMAL_TYPES = {"Float", "Double", "BigDecimal"}
UPPER_BOUNDS = {"maxLength", "maximum", "maxItems"}
LOWER_BOUNDS = {"minLength", "minimum", "minItems"}


@dataclass
class Finding:
    id: str
    scope: str            # "model" or "spec"
    category: str         # Missing, Renamed, Type, Required and nullable, Enum values, Constraints and defaults, Documentation, Spec
    where: str
    what: str
    left: str | None
    right: str | None
    impact: str
    reason: str
    cls: str | None = None
    field: str | None = None
    aspect: str | None = None
    overridden: bool = False
    note: str | None = None


@dataclass
class Context:
    label1: str
    label2: str
    baseline: int | None          # 1 or 2 = that side is the older contract
    findings: list[Finding] = field(default_factory=list)

    def add(self, **kw) -> Finding:
        f = Finding(id=str(len(self.findings) + 1), **kw)
        self.findings.append(f)
        return f

    def old_is_left(self) -> bool | None:
        return None if self.baseline is None else self.baseline == 1


# --------------------------------------------------------------------------- impact rules

def rate_model_diff(ctx: Context, aspect: str, left: str | None, right: str | None) -> tuple[str, str, str]:
    """Return (category, impact, reason) for one model-level diff."""
    l1, l2 = ctx.label1, ctx.label2
    if aspect == "class.renamed":
        return "Renamed", SAFE, "Same JSON on the wire. Java code that names the class must change."
    if aspect == "class.superclass":
        return "Type", RISKY, "Class hierarchy differs. Code that relies on the parent type breaks."
    if aspect in ("enum.onlyLeft", "enum.onlyRight"):
        return "Enum values", BREAKING, "Generated enums throw on unknown values, so a client fails when the other side sends this value."
    if aspect == "property.renamed":
        return "Renamed", BREAKING, "The JSON field name changed. Each side misses the other's field."
    if aspect in ("property.onlyLeft", "property.onlyRight"):
        present = left if aspect == "property.onlyLeft" else right
        if present and present.endswith("(required)"):
            return "Missing", BREAKING, "A required property exists on one side only."
        old_left = ctx.old_is_left()
        if old_left is not None:
            removed = (aspect == "property.onlyLeft") == old_left
            if removed:
                return "Missing", BREAKING, "An optional property was removed. Clients that send it lose the data, and clients that read it get nothing."
            return "Missing", RISKY, "An optional property was added. Tolerant clients ignore it, strict deserializers reject it."
        return "Missing", RISKY, "An optional property exists on one side only. Data sent by one side is dropped by the other, and strict deserializers reject unknown fields."
    if aspect == "property.type":
        if {left, right} <= INTEGER_TYPES or {left, right} <= DECIMAL_TYPES:
            return "Type", SAFE, "Same JSON number, different Java width. Wire-compatible within range, but Java callers must change type."
        return "Type", BREAKING, "The wire representation differs. Each side rejects or misreads the other's value."
    if aspect == "property.required":
        old_left = ctx.old_is_left()
        if old_left is not None:
            old_val = left if old_left else right
            if old_val == "false":
                return "Required and nullable", BREAKING, "Became required. Requests that omit it are now rejected."
            return "Required and nullable", RISKY, "Became optional. Clients that assume it's always present may fail."
        return "Required and nullable", BREAKING, f"Required on one side only. Payloads valid for one side fail validation on the other."
    if aspect == "property.nullable":
        return "Required and nullable", RISKY, "One side can send null where the other doesn't expect it."
    if aspect == "property.readOnly":
        return "Required and nullable", RISKY, "readOnly differs. One side accepts the field in requests and the other ignores or rejects it."
    if aspect == "property.default":
        return "Constraints and defaults", BREAKING, "When a client omits the field, the two sides behave differently."
    if aspect.startswith("constraint."):
        name = aspect.split(".", 1)[1]
        if left is None or right is None:
            return "Constraints and defaults", RISKY, "The constraint exists on one side only. Values one side accepts may be rejected by the other."
        if name in UPPER_BOUNDS | LOWER_BOUNDS:
            try:
                lv, rv = float(left), float(right)
            except ValueError:
                return "Constraints and defaults", BREAKING, "The bound differs."
            old_left = ctx.old_is_left()
            if old_left is not None:
                old_v, new_v = (lv, rv) if old_left else (rv, lv)
                tightened = new_v < old_v if name in UPPER_BOUNDS else new_v > old_v
                if not tightened:
                    return "Constraints and defaults", RISKY, "The bound was loosened. The new side accepts values that old clients may not handle."
            hi, lo = (max(lv, rv), min(lv, rv))
            fmt = lambda v: str(int(v)) if v.is_integer() else str(v)
            if name in UPPER_BOUNDS:
                return "Constraints and defaults", BREAKING, f"Values above {fmt(lo)} and up to {fmt(hi)} are accepted by one side and rejected by the other."
            return "Constraints and defaults", BREAKING, f"Values from {fmt(lo)} up to below {fmt(hi)} are accepted by one side and rejected by the other."
        return "Constraints and defaults", BREAKING, f"The {name} constraint differs, so the two sides validate differently."
    if aspect.startswith("doc."):
        return "Documentation", COSMETIC, "Documentation only."
    return "Other", RISKY, "Unclassified difference. Review it manually."


ASPECT_LABEL = {
    "class.renamed": "Class name", "class.superclass": "Superclass",
    "enum.onlyLeft": "Enum value only in {l1}", "enum.onlyRight": "Enum value only in {l2}",
    "property.renamed": "JSON property name", "property.onlyLeft": "Property only in {l1}",
    "property.onlyRight": "Property only in {l2}", "property.type": "Java type",
    "property.required": "Required", "property.nullable": "Nullable", "property.readOnly": "readOnly",
    "property.default": "Default", "doc.description": "Description", "doc.example": "Example",
}


def aspect_label(aspect: str, ctx: Context) -> str:
    if aspect.startswith("constraint."):
        return aspect.split(".", 1)[1]
    return ASPECT_LABEL.get(aspect, aspect).format(l1=ctx.label1, l2=ctx.label2)


# --------------------------------------------------------------------------- spec-level

def schemas_used_by_paths(spec: dict) -> set[str]:
    text = json.dumps(spec.get("paths", {}))
    return {n for n in spec.get("components", {}).get("schemas", {}) if f'"#/components/schemas/{n}"' in text}


def operations(spec: dict) -> dict[tuple[str, str], dict]:
    ops = {}
    for path, item in spec.get("paths", {}).items():
        shared = item.get("parameters", [])
        for method, op in item.items():
            if method in ("get", "put", "post", "delete", "patch", "head", "options", "trace"):
                op = dict(op)
                op["parameters"] = shared + op.get("parameters", [])
                ops[(method.upper(), path)] = op
    return ops


def resolve_param(p: dict, spec: dict) -> dict:
    ref = p.get("$ref")
    if ref and ref.startswith("#/components/parameters/"):
        return spec.get("components", {}).get("parameters", {}).get(ref.rsplit("/", 1)[1], p)
    return p


def schema_sig(s: dict | None) -> str:
    s = s or {}
    return f"{s.get('type', s.get('$ref', '?'))}{'/' + s['format'] if 'format' in s else ''}"


def compare_specs(ctx: Context, s1: dict, s2: dict) -> None:
    v1, v2 = s1.get("info", {}).get("version"), s2.get("info", {}).get("version")
    if v1 != v2:
        ctx.add(scope="spec", category="Spec", where="info.version", what="API version", left=v1, right=v2,
                impact=COSMETIC, reason="Version labels differ. Check the bump matches the breaking changes reported.")
    if s1.get("servers") != s2.get("servers"):
        ctx.add(scope="spec", category="Spec", where="servers", what="Server URLs",
                left=", ".join(x.get("url", "") for x in s1.get("servers", [])),
                right=", ".join(x.get("url", "") for x in s2.get("servers", [])), impact=COSMETIC, reason="Deployment detail.")
    if s1.get("security") != s2.get("security"):
        ctx.add(scope="spec", category="Spec", where="security", what="Global security requirement",
                left=json.dumps(s1.get("security")), right=json.dumps(s2.get("security")), impact=RISKY,
                reason="Authentication requirements differ.")

    o1, o2 = operations(s1), operations(s2)
    for key in sorted(set(o1) | set(o2)):
        where = f"{key[0]} {key[1]}"
        if key not in o2 or key not in o1:
            ctx.add(scope="spec", category="Spec", where=where, what="Operation",
                    left="present" if key in o1 else None, right="present" if key in o2 else None,
                    impact=BREAKING, reason="The endpoint exists on one side only.")
            continue
        a, b = o1[key], o2[key]
        if a.get("operationId") != b.get("operationId"):
            ctx.add(scope="spec", category="Spec", where=where, what="operationId",
                    left=a.get("operationId") or "missing (auto-named)", right=b.get("operationId") or "missing (auto-named)",
                    impact=SAFE, reason="Same wire contract, but generated client and server method names differ.")
        pa = {(p["name"], p["in"]): p for p in (resolve_param(x, s1) for x in a["parameters"]) if "name" in p}
        pb = {(p["name"], p["in"]): p for p in (resolve_param(x, s2) for x in b["parameters"]) if "name" in p}
        only_a, only_b = sorted(set(pa) - set(pb)), sorted(set(pb) - set(pa))
        for ka in list(only_a):   # rename detection: same location, schema, required flag
            for kb in list(only_b):
                if ka[1] == kb[1] and schema_sig(pa[ka].get("schema")) == schema_sig(pb[kb].get("schema")) \
                        and bool(pa[ka].get("required")) == bool(pb[kb].get("required")):
                    ctx.add(scope="spec", category="Spec", where=f"{where} ({ka[1]} parameter)", what="Parameter renamed",
                            left=ka[0], right=kb[0], impact=BREAKING,
                            reason="Clients that send the old name are ignored or rejected. An unknown query parameter is usually ignored silently.")
                    only_a.remove(ka); only_b.remove(kb)
                    break
        for k, side in [(k, "l") for k in only_a] + [(k, "r") for k in only_b]:
            p = pa[k] if side == "l" else pb[k]
            req = bool(p.get("required"))
            ctx.add(scope="spec", category="Spec", where=f"{where} ({k[1]} parameter)", what="Parameter",
                    left=k[0] if side == "l" else None, right=k[0] if side == "r" else None,
                    impact=BREAKING if req else RISKY,
                    reason="A required parameter exists on one side only." if req else "An optional parameter exists on one side only.")
        for k in sorted(set(pa) & set(pb)):
            if bool(pa[k].get("required")) != bool(pb[k].get("required")):
                ctx.add(scope="spec", category="Spec", where=f"{where} ({k[1]} parameter {k[0]})", what="Parameter required",
                        left=str(bool(pa[k].get("required"))).lower(), right=str(bool(pb[k].get("required"))).lower(),
                        impact=BREAKING, reason="Required on one side only.")
            if schema_sig(pa[k].get("schema")) != schema_sig(pb[k].get("schema")):
                ctx.add(scope="spec", category="Spec", where=f"{where} ({k[1]} parameter {k[0]})", what="Parameter type",
                        left=schema_sig(pa[k].get("schema")), right=schema_sig(pb[k].get("schema")),
                        impact=BREAKING, reason="The parameter's type or format differs.")
        ra, rb = set(a.get("responses", {})), set(b.get("responses", {}))
        for code in sorted(ra ^ rb):
            ctx.add(scope="spec", category="Spec", where=where, what=f"Response {code}",
                    left="declared" if code in ra else None, right="declared" if code in rb else None,
                    impact=RISKY, reason="A response status is documented on one side only.")
        qa, qb = a.get("requestBody"), b.get("requestBody")
        if bool(qa) != bool(qb) or bool((qa or {}).get("required")) != bool((qb or {}).get("required")):
            ctx.add(scope="spec", category="Spec", where=where, what="Request body",
                    left="required" if (qa or {}).get("required") else ("optional" if qa else None),
                    right="required" if (qb or {}).get("required") else ("optional" if qb else None),
                    impact=BREAKING, reason="Request body presence or required flag differs.")


# --------------------------------------------------------------------------- model-level

def compare_models(ctx: Context, diff: dict, used1: set[str], used2: set[str]) -> list[dict]:
    rows = []
    for side, key, used in (("left", "onlyLeft", used1), ("right", "onlyRight", used2)):
        for m in diff[key]:
            label = ctx.label1 if side == "left" else ctx.label2
            if m["name"] in used:
                impact, reason = BREAKING, f"Used directly by an endpoint in {label} but absent from the other spec."
            elif m["referencedBy"]:
                impact, reason = RISKY, f"Referenced by {', '.join(m['referencedBy'])}. The impact is rated on that property."
            else:
                impact, reason = COSMETIC, "Not referenced by any endpoint or other model. Usually a leftover allOf base or an unused schema."
            shape = ", ".join(m["props"]) if m["props"] else ", ".join(m["enum"])
            ctx.add(scope="model", category="Missing", where=m["name"], what=f"Schema only in {label}",
                    left=shape if side == "left" else None, right=shape if side == "right" else None,
                    impact=impact, reason=reason, cls=m["name"], aspect=f"class.{key}")
            rows.append({"left": m["name"] if side == "left" else None, "right": m["name"] if side == "right" else None,
                         "result": "only", "note": reason, "ids": [ctx.findings[-1].id]})

    for p in diff["pairs"]:
        ids = []
        for d in p["diffs"]:
            cat, impact, reason = rate_model_diff(ctx, d["aspect"], d["left"], d["right"])
            where = p["left"] if p["left"] == p["right"] else f"{p['left']} / {p['right']}"
            if d["field"]:
                fld = d["field"] if d["aspect"] != "property.renamed" else f"{d['left']} → {d['right']}"
                where = f"{where}.{fld}"
            f = ctx.add(scope="model", category=cat, where=where, what=aspect_label(d["aspect"], ctx),
                        left=d["left"], right=d["right"], impact=impact, reason=reason,
                        cls=p["left"], field=d["field"], aspect=d["aspect"])
            ids.append(f.id)
        rows.append({"left": p["left"], "right": p["right"], "matchedBy": p["matchedBy"], "score": p["score"],
                     "equalProps": p["equalProps"], "propsLeft": p["propsLeft"], "propsRight": p["propsRight"],
                     "kind": p["kind"], "ids": ids})
    return rows


def classify_rows(rows: list[dict], by_id: dict[str, Finding]) -> None:
    for r in rows:
        if r.get("result") == "only":
            continue
        impacts = {by_id[i].impact for i in r["ids"]}
        if not impacts:
            r["result"] = "identical"
        elif impacts <= {COSMETIC, SAFE}:
            r["result"] = "equivalent"
        else:
            r["result"] = "different"


def apply_overrides(ctx: Context, path: str | None) -> None:
    if not path:
        return
    rules = json.loads(Path(path).read_text())
    for rule in rules:
        match = rule.get("match", {})
        for f in ctx.findings:
            if all(str(getattr(f, k if k != "class" else "cls", None)) == str(v) for k, v in match.items()):
                if "impact" in rule:
                    if rule["impact"] not in IMPACT_ORDER:
                        sys.exit(f"overrides: unknown impact {rule['impact']!r}")
                    f.impact, f.overridden = rule["impact"], True
                if "note" in rule:
                    f.note = rule["note"]


# --------------------------------------------------------------------------- HTML

CSS = """
:root{--bg:#fff;--fg:#1d1f23;--muted:#5d636e;--line:#e3e5e8;--card:#f6f7f9;--err:#b42318;--warn:#b54708;--info:#175cd3;--ok:#067647;--code:#f1f3f5}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#15171a;--fg:#e7e9ec;--muted:#9aa1ab;--line:#2c3036;--card:#1c1f23;--err:#f97066;--warn:#fdb022;--info:#84adff;--ok:#47cd89;--code:#23272c}}
body{background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:24px 16px 64px}
main{max-width:1080px;margin:0 auto}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 10px}h3{font-size:15px;margin:16px 0 4px}
.muted{color:var(--muted)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:18px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}.card b{display:block;font-size:22px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;vertical-align:top;padding:8px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600}code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;background:var(--code);border-radius:4px;padding:1px 4px}
.pill{display:inline-block;font-size:11.5px;font-weight:700;border-radius:10px;padding:1px 8px;white-space:nowrap}
.identical{color:var(--ok);border:1px solid var(--ok)}.equivalent{color:var(--info);border:1px solid var(--info)}
.different{color:var(--warn);border:1px solid var(--warn)}.only{color:var(--err);border:1px solid var(--err)}
.breaking{color:var(--err);font-weight:700}.risky{color:var(--warn);font-weight:700}.safe{color:var(--ok);font-weight:700}.cosmetic{color:var(--muted);font-weight:700}
.wrap{overflow-x:auto}.note{background:var(--card);border-left:3px solid var(--info);padding:10px 12px;border-radius:4px;margin:12px 0}
"""


def e(v) -> str:
    return "<span class='muted'>none</span>" if v is None else html.escape(str(v))


def c(v) -> str:
    return "<span class='muted'>none</span>" if v is None else f"<code>{html.escape(str(v))}</code>"


def render(ctx: Context, rows: list[dict], meta: dict, counts: dict, verdict: str) -> str:
    l1, l2 = html.escape(ctx.label1), html.escape(ctx.label2)
    by_id = {f.id: f for f in ctx.findings}
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
           f"<title>Model Equivalence Report</title><style>{CSS}</style></head><body><main>",
           "<h1>Model equivalence report</h1>",
           f"<p class='muted'>Generated {meta['generated']} · openapi-generator {html.escape(meta['generator'])} (spring, models only)</p>",
           "<table style='max-width:820px'>",
           f"<tr><th>{l1}</th><td>{c(meta['source1'])}</td></tr>",
           f"<tr><th>{l2}</th><td>{c(meta['source2'])}</td></tr>",
           f"<tr><th>Impact direction</th><td>{e(meta['direction'])}</td></tr></table>"]

    out.append("<div class='cards'>")
    for label, val, color in [("Class pairs", counts["pairs"], "var(--fg)"), ("Identical", counts["identical"], "var(--ok)"),
                              ("Equivalent", counts["equivalent"], "var(--info)"), ("Different", counts["different"], "var(--warn)"),
                              ("Only in one spec", counts["only"], "var(--err)"), ("Breaking", counts[BREAKING], "var(--err)"),
                              ("Risky", counts[RISKY], "var(--warn)")]:
        out.append(f"<div class='card'><span class='muted'>{label}</span><b style='color:{color}'>{val}</b></div>")
    out.append("</div>")
    out.append(f"<h2>1. Verdict</h2><p><strong>{html.escape(verdict)}</strong></p>")

    out.append("<h2>2. Class-by-class summary</h2><div class='wrap'><table><thead><tr>"
               f"<th>{l1}</th><th>{l2}</th><th>Result</th><th>Notes</th></tr></thead><tbody>")
    order = {"different": 0, "only": 1, "equivalent": 2, "identical": 3}
    for r in sorted(rows, key=lambda r: (order[r["result"]], r["left"] or r["right"])):
        label = {"identical": "Identical", "equivalent": "Equivalent", "different": "Different", "only": "Only in one spec"}[r["result"]]
        if r["result"] == "only":
            note = html.escape(r["note"])
        elif r["result"] == "identical":
            note = f"All {r['propsLeft']} properties match." if r["kind"] == "class" else "Same enum values."
        else:
            refs = ", ".join(f"#{i}" for i in r["ids"])
            note = f"{len(r['ids'])} difference(s): {refs}."
            if r["kind"] == "class":
                note += f" {len(r['equalProps'])} of {max(r['propsLeft'], r['propsRight'])} properties match."
            if r.get("matchedBy") == "structure":
                note += f" Paired by structure ({float(r['score']) * 100:.0f}% match)."
        out.append(f"<tr><td>{c(r['left'])}</td><td>{c(r['right'])}</td><td><span class='pill {r['result']}'>{label}</span></td><td>{note}</td></tr>")
    out.append("</tbody></table></div>")

    def table(findings: list[Finding], title: str) -> None:
        out.append(f"<h2>{title}</h2>")
        if not findings:
            out.append("<p class='muted'>None.</p>")
            return
        out.append("<p class='muted'>Impact: <span class='breaking'>Breaking</span> fails or loses data on the wire · "
                   "<span class='risky'>Risky</span> breaks some clients or changes behaviour · "
                   "<span class='safe'>Safe</span> wire-compatible but needs code changes · <span class='cosmetic'>Cosmetic</span> docs only</p>")
        out.append(f"<div class='wrap'><table><thead><tr><th>#</th><th>Where</th><th>What differs</th><th>{l1}</th><th>{l2}</th><th>Impact</th></tr></thead><tbody>")
        cats = []
        for f in findings:
            if f.category not in cats:
                cats.append(f.category)
        for cat in cats:
            if len(cats) > 1:
                out.append(f"<tr><td colspan='6'><h3>{html.escape(cat)}</h3></td></tr>")
            for f in sorted((x for x in findings if x.category == cat), key=lambda x: (IMPACT_ORDER[x.impact], int(x.id))):
                extra = " <span class='muted'>(overridden)</span>" if f.overridden else ""
                note = f"<br><span class='muted'>Note: {html.escape(f.note)}</span>" if f.note else ""
                out.append(f"<tr><td>{f.id}</td><td>{c(f.where)}</td><td>{html.escape(f.what)}</td><td>{c(f.left)}</td><td>{c(f.right)}</td>"
                           f"<td><span class='{f.impact}'>{f.impact.capitalize()}</span>{extra}. {html.escape(f.reason)}{note}</td></tr>")
        out.append("</tbody></table></div>")

    cat_order = ["Missing", "Renamed", "Type", "Required and nullable", "Enum values", "Constraints and defaults", "Documentation", "Other"]
    model_f = sorted((f for f in ctx.findings if f.scope == "model"), key=lambda f: (cat_order.index(f.category), int(f.id)))
    table(model_f, "3. Model differences")
    table([f for f in ctx.findings if f.scope == "spec"], "4. Spec-level differences (outside the models)")

    out.append("<h2>5. Equivalent properties</h2><p class='muted'>Same JSON name, type, required flag, nullability, constraints, default and docs on both sides.</p><ul>")
    for r in rows:
        if r.get("equalProps"):
            name = r["left"] if r["left"] == r["right"] else f"{r['left']} / {r['right']}"
            out.append(f"<li><code>{html.escape(name)}</code>: {html.escape(', '.join(r['equalProps']))}</li>")
    out.append("</ul>")
    out.append("<h2>6. Method</h2><p class='muted'>Both specs were generated with identical settings and compiled. The classes were compared by reflection "
               "(JSON name, Java type, required, nullable, readOnly, Bean Validation constraints, defaults, description, example). "
               "Renamed classes and properties were paired by structure. Spec-level differences come from the normalized single-file specs. "
               "Impacts come from a fixed rule table in <code>render_report.py</code>. Overridden findings are marked.</p>")
    out.append("</main></body></html>")
    return "\n".join(out)


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--diff", required=True)
    ap.add_argument("--spec-json1", required=True)
    ap.add_argument("--spec-json2", required=True)
    ap.add_argument("--label1", default="spec1")
    ap.add_argument("--label2", default="spec2")
    ap.add_argument("--source1", default="")
    ap.add_argument("--source2", default="")
    ap.add_argument("--generator-version", default="unknown")
    ap.add_argument("--baseline", type=int, choices=[1, 2])
    ap.add_argument("--overrides")
    ap.add_argument("--out", required=True)
    ap.add_argument("--summary", required=True)
    ap.add_argument("--fail-on-breaking", action="store_true")
    a = ap.parse_args()

    try:
        diff = json.loads(Path(a.diff).read_text())
        s1, s2 = json.loads(Path(a.spec_json1).read_text()), json.loads(Path(a.spec_json2).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"render_report: cannot read input: {exc}", file=sys.stderr)
        return 2

    ctx = Context(a.label1, a.label2, a.baseline)
    rows = compare_models(ctx, diff, schemas_used_by_paths(s1), schemas_used_by_paths(s2))
    compare_specs(ctx, s1, s2)
    apply_overrides(ctx, a.overrides)
    by_id = {f.id: f for f in ctx.findings}
    classify_rows(rows, by_id)

    counts = {k: sum(1 for r in rows if r["result"] == k) for k in ("identical", "equivalent", "different", "only")}
    counts["pairs"] = len(diff["pairs"])
    for imp in IMPACT_ORDER:
        counts[imp] = sum(1 for f in ctx.findings if f.impact == imp)
    if counts[BREAKING]:
        verdict = (f"Not equivalent. {counts[BREAKING]} breaking difference(s): a client built from one spec would fail "
                   f"or lose data against a server built from the other.")
    elif counts[RISKY]:
        verdict = f"Compatible with caveats. No breaking differences, but {counts[RISKY]} risky one(s) need review."
    elif ctx.findings:
        verdict = "Wire-equivalent. Only code-level or documentation differences."
    else:
        verdict = "Identical. No differences found."

    if a.baseline:
        old, new = (a.label1, a.label2) if a.baseline == 1 else (a.label2, a.label1)
        direction = f"{old} is the baseline (older contract) and {new} is the change."
    else:
        direction = "No baseline given, so differences are rated as incompatible in either direction."
    meta = {"generated": dt.date.today().isoformat(), "generator": a.generator_version,
            "source1": a.source1 or a.label1, "source2": a.source2 or a.label2, "direction": direction}

    Path(a.out).write_text(render(ctx, rows, meta, counts, verdict))
    Path(a.summary).write_text(json.dumps({"verdict": verdict, "counts": counts, "meta": meta,
                                           "classes": rows, "findings": [asdict(f) for f in ctx.findings]}, indent=2))
    print(f"{verdict}\n  classes: {counts['identical']} identical, {counts['equivalent']} equivalent, "
          f"{counts['different']} different, {counts['only']} only in one spec\n"
          f"  findings: {counts[BREAKING]} breaking, {counts[RISKY]} risky, {counts[SAFE]} safe, {counts[COSMETIC]} cosmetic")
    return 1 if a.fail_on_breaking and counts[BREAKING] else 0


if __name__ == "__main__":
    sys.exit(main())
