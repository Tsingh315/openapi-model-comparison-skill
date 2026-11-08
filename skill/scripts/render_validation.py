#!/usr/bin/env python3
"""Render a validation report (HTML) for one OpenAPI spec.

Combines the openapi-generator validator output (facts) with the manual review
findings (judgment), which Claude writes as a JSON file:

{
  "findings": [
    {"id": "S1-1", "severity": "defect|warning|suggestion",
     "location": "schemas/Address.yaml:19",
     "issue": "What is wrong and why it matters.",
     "fix": "What to change (plain text; YAML snippets allowed).",
     "changesComparison": false}
  ],
  "checkedOk": ["Every $ref resolves.", "..."]
}

"changesComparison": true marks a fix that would also remove or change a
difference against the other spec.

Usage:
  render_validation.py --label source1 --spec path/openapi.yaml \
      --cli-output validation/source1-cli.txt [--findings findings.json] --out report.html

Standard library only. Exit codes: 0 ok, 2 bad input.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import sys
from pathlib import Path

SEVERITIES = ("defect", "warning", "suggestion")

CSS = """
:root{--bg:#fff;--fg:#1d1f23;--muted:#5d636e;--line:#e3e5e8;--card:#f6f7f9;--err:#b42318;--warn:#b54708;--info:#175cd3;--ok:#067647;--code:#f1f3f5}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#15171a;--fg:#e7e9ec;--muted:#9aa1ab;--line:#2c3036;--card:#1c1f23;--err:#f97066;--warn:#fdb022;--info:#84adff;--ok:#47cd89;--code:#23272c}}
body{background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;margin:0;padding:24px 16px 64px}
main{max-width:1000px;margin:0 auto}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 10px}.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:18px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}.card b{display:block;font-size:22px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;vertical-align:top;padding:9px 8px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600}code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;background:var(--code);border-radius:4px}
code{padding:1px 4px}pre{padding:8px 10px;margin:6px 0 0;white-space:pre-wrap}
.sev{font-weight:700;font-size:12px;text-transform:uppercase}.defect{color:var(--err)}.warning{color:var(--warn)}.suggestion{color:var(--info)}.ok{color:var(--ok)}
.tag{display:inline-block;font-size:11px;border:1px solid var(--warn);color:var(--warn);border-radius:10px;padding:0 7px;margin-top:4px}.wrap{overflow-x:auto}
"""


def fmt(text: str) -> str:
    """Escape text; render blank-line-separated or indented blocks as <pre>."""
    t = html.escape(text)
    return f"<pre>{t}</pre>" if "\n" in t else t


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--cli-output", required=True)
    ap.add_argument("--findings")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    try:
        cli = Path(a.cli_output).read_text().strip()
        data = json.loads(Path(a.findings).read_text()) if a.findings else {"findings": [], "checkedOk": []}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"render_validation: cannot read input: {exc}", file=sys.stderr)
        return 2
    findings = data.get("findings", [])
    for f in findings:
        if f.get("severity") not in SEVERITIES:
            print(f"render_validation: finding {f.get('id')} has invalid severity {f.get('severity')!r}", file=sys.stderr)
            return 2

    passed = "No validation issues detected" in cli
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}
    label = html.escape(a.label)
    out = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
           f"<title>{label.capitalize()} Validation Report</title><style>{CSS}</style></head><body><main>",
           f"<h1>{label} validation report</h1>",
           f"<p class='muted'>Spec: <code>{html.escape(a.spec)}</code> · Generated {dt.date.today().isoformat()}</p>",
           "<div class='cards'>",
           f"<div class='card'><span class='muted'>Structural validation</span><b class='{'ok' if passed else 'defect'}'>{'Pass' if passed else 'Fail'}</b></div>"]
    for s in SEVERITIES:
        out.append(f"<div class='card'><span class='muted'>{s.capitalize()}s</span><b class='{s if counts[s] else 'ok'}'>{counts[s]}</b></div>")
    out.append("</div>")

    out.append(f"<h2>1. Tool result</h2><pre>{html.escape(cli)}</pre>")
    out.append("<h2>2. Review findings</h2>")
    if not findings:
        out.append("<p class='muted'>No manual review findings were recorded.</p>")
    else:
        if any(f.get("changesComparison") for f in findings):
            out.append("<p class='muted'>Findings tagged <span class='tag'>changes a difference vs the other spec</span> would also "
                       "remove or change a difference from the other spec. Applying them changes what the equivalence report shows.</p>")
        out.append("<div class='wrap'><table><thead><tr><th>#</th><th>Severity</th><th>Location</th><th>Issue</th><th>Recommended fix</th></tr></thead><tbody>")
        for f in sorted(findings, key=lambda f: SEVERITIES.index(f["severity"])):
            tag = "<br><span class='tag'>changes a difference vs the other spec</span>" if f.get("changesComparison") else ""
            out.append(f"<tr><td>{html.escape(f.get('id', ''))}</td><td class='sev {f['severity']}'>{f['severity']}</td>"
                       f"<td><code>{html.escape(f.get('location', ''))}</code></td><td>{fmt(f.get('issue', ''))}</td>"
                       f"<td>{fmt(f.get('fix', ''))}{tag}</td></tr>")
        out.append("</tbody></table></div>")
    if data.get("checkedOk"):
        out.append("<h2>3. Checked with no issues</h2><ul>")
        out.extend(f"<li>{html.escape(x)}</li>" for x in data["checkedOk"])
        out.append("</ul>")
    out.append("</main></body></html>")
    Path(a.out).write_text("\n".join(out))
    print(f"{a.label}: structural {'pass' if passed else 'FAIL'}; "
          f"{counts['defect']} defects, {counts['warning']} warnings, {counts['suggestion']} suggestions -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
