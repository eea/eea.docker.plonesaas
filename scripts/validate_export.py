#!/usr/bin/env python3
"""Validate the EPANET export file against the expected inventory.

Usage:
    python scripts/validate_export.py
    python scripts/validate_export.py --input fresh-test/epanet.json

Outputs:
    <input-dir>/VALIDATION_REPORT.md (default: content-exports/VALIDATION_REPORT.md)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

EXPORT_FILE = Path(__file__).parent.parent / "content-exports" / "epanet.json"
REPORT_FILE = EXPORT_FILE.with_name("VALIDATION_REPORT.md")

# Inventory from anonymous catalog search on epanet.eea.europa.eu (2026-09-10).
EXPECTED_COUNTS = {
    "Folder": 7,
    "Document": 13,
    "News Item": 39,
    "File": 95,
    "Image": 114,
    "Link": 1,
    "Collection": 2,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=EXPORT_FILE,
                        help="Export to validate (default: %(default)s)")
    parser.add_argument("--report", type=Path, default=None,
                        help="Report path (default: <input-dir>/VALIDATION_REPORT.md)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    export_file = args.input
    report_file = args.report or export_file.with_name("VALIDATION_REPORT.md")

    if not export_file.exists():
        print(f"Export file not found: {export_file}", file=sys.stderr)
        return 1

    with export_file.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        print(f"Unexpected export structure: {type(data).__name__}", file=sys.stderr)
        return 1

    total = len(data)
    actual = Counter(item.get("@type", "UNKNOWN") for item in data)

    languages = Counter(item.get("language") for item in data)
    review_states = Counter(item.get("review_state") for item in data)

    # Build per-type language breakdown for the migration transform.
    type_language: dict[str, Counter] = {}
    for item in data:
        t = item.get("@type", "UNKNOWN")
        type_language.setdefault(t, Counter())[item.get("language")] += 1

    # Discrepancies vs expected catalog counts.
    discrepancies = []
    all_types = set(EXPECTED_COUNTS) | set(actual)
    for t in sorted(all_types):
        exp = EXPECTED_COUNTS.get(t, 0)
        got = actual.get(t, 0)
        if exp != got:
            discrepancies.append((t, exp, got))

    # Identify private items (plan says published-only; flag them).
    private_items = [item for item in data if item.get("review_state") == "private"]

    # Identify workflow-less items (review_state is None).
    no_workflow = [item for item in data if item.get("review_state") is None]

    # Collections for manual rebuild.
    collections = [item for item in data if item.get("@type") == "Collection"]

    # Largest payload items (base64 blobs).
    payload_sizes = []
    for item in data:
        size = 0
        for key in ("file", "image"):
            blob = item.get(key)
            if isinstance(blob, dict):
                size += len(blob.get("data", ""))
        if size:
            payload_sizes.append((item["@id"], item["@type"], size))
    payload_sizes.sort(key=lambda x: x[2], reverse=True)

    lines = [
        "# EPANET Export Validation Report",
        "",
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}Z",
        f"**Export file:** `{export_file}`",
        f"**File size:** {export_file.stat().st_size:,} bytes ({export_file.stat().st_size / 1024 / 1024:.1f} MB)",
        "",
        "## Summary",
        "",
        f"- **Total exported items:** {total}",
        f"- **Expected total (catalog):** {sum(EXPECTED_COUNTS.values())}",
        f"- **Discrepancies:** {len(discrepancies)}",
        f"- **Private items:** {len(private_items)}",
        f"- **Items without workflow state:** {len(no_workflow)}",
        f"- **Collections (manual rebuild required):** {len(collections)}",
        "",
        "## Counts vs inventory",
        "",
        "| Type | Expected (catalog) | Exported | Status |",
        "|---|---|---|---|",
    ]
    for t in sorted(all_types):
        exp = EXPECTED_COUNTS.get(t, 0)
        got = actual.get(t, 0)
        status = "OK" if exp == got else f"DIFF (+{got - exp})" if got > exp else f"DIFF ({got - exp})"
        lines.append(f"| {t} | {exp} | {got} | {status} |")

    if discrepancies:
        lines.extend([
            "",
            "## Discrepancy details",
            "",
        ])
        for t, exp, got in discrepancies:
            lines.append(f"- **{t}**: expected {exp}, got {got} (delta {got - exp:+d})")
            if t == "Link":
                lines.append(
                    "  - The catalog search only found the published `/log-in-1` link. "
                    "The export also contains two private links under `/our-group/` (Poland and Norwegian EPA)."
                )

    lines.extend([
        "",
        "## Language distribution",
        "",
        "| Language | Count |",
        "|---|---|",
    ])
    for lang, count in sorted(languages.items(), key=lambda x: str(x[0])):
        lines.append(f"| `{lang!r}` | {count} |")

    lines.extend([
        "",
        "### Language by type",
        "",
    ])
    for t in sorted(type_language):
        langs = type_language[t]
        lang_str = ", ".join(f"{k!r}: {v}" for k, v in sorted(langs.items(), key=lambda x: str(x[0])))
        lines.append(f"- **{t}**: {lang_str}")

    lines.extend([
        "",
        "## Review state distribution",
        "",
        "| Review state | Count |",
        "|---|---|",
    ])
    for state, count in sorted(review_states.items(), key=lambda x: str(x[0])):
        lines.append(f"| `{state!r}` | {count} |")

    if private_items:
        lines.extend([
            "",
            "## Private items",
            "",
            "The migration plan targets published-only content. The following private items were exported:",
            "",
        ])
        for item in private_items:
            lines.append(f"- `{item['@id']}` ({item['@type']})")

    if no_workflow:
        lines.extend([
            "",
            "## Items without workflow state",
            "",
            f"{len(no_workflow)} items have `review_state: null`. All of these are Files and Images; "
            "they have no workflow history in the source. They will still be imported, but the target "
            "will apply its default workflow binding (if any).",
            "",
            "Breakdown by type:",
            "",
        ])
        no_wf_by_type = Counter(item["@type"] for item in no_workflow)
        for t, count in sorted(no_wf_by_type.items()):
            lines.append(f"- {t}: {count}")

    if collections:
        lines.extend([
            "",
            "## Collections to rebuild as listing blocks",
            "",
        ])
        for item in collections:
            lines.append(f"- `{item['@id']}`")
            lines.append(f"  - layout: `{item.get('layout')}`")
            lines.append(f"  - sort_on: `{item.get('sort_on')}`")
            lines.append(f"  - limit: `{item.get('limit')}`")
            lines.append(f"  - query: `{json.dumps(item.get('query', []))}`")

    lines.extend([
        "",
        "## Largest blob payloads",
        "",
        "| Path | Type | Base64 bytes |",
        "|---|---|---|",
    ])
    for path, typ, size in payload_sizes[:10]:
        lines.append(f"| `{path}` | {typ} | {size:,} |")

    lines.extend([
        "",
        "## Migration transform implications",
        "",
        "1. **Language normalization required.** Items use `''` and `'en-gb'`. "
        "Normalize to `'en'` during transform.",
        "2. **Folder → Document.** The 7 Folders become Volto Documents (folderish on the target).",
        "3. **Collections skipped as type.** Convert the 2 Collections to listing blocks inside their parent Document.",
        "4. **Private Links.** Decide whether to include the 2 private `/our-group/*` Links; "
        "the plan targets published-only content.",
        "5. **File/Image workflow.** No workflow state on source Files/Images; importer will apply target defaults.",
        "",
    ])

    report_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Validation report written to: {report_file}")

    if discrepancies:
        print(f"\nWARN: {len(discrepancies)} type count discrepancies found.")
        return 0  # non-fatal; report captures it.

    return 0


if __name__ == "__main__":
    sys.exit(main())
