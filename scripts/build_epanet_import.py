#!/usr/bin/env python3
"""Build a single import file, epanet-import.json, from one EPANET export.

Runs the migration end to end and writes ONE JSON to import with
handle_existing_content=2 ("Update existing content"):

    1  validate      scripts/validate_export.py      -> VALIDATION_REPORT.md
    2  transform     scripts/epanet_migrate.py       -> epanet-transformed.json
    3  align         scripts/fix_news_image_alignment.py (JSON mode, with the
                     resolveuid fix applied in-process)
                                                     -> epanet-transformed-fixed.json
    4  teasers       scripts/build_teaser_grid.py    -> epanet-transformed-fixed-teasers.json
    5  combine       front-page blocks -> Subsite item (first), front-page dropped;
                     review_state restored from the export; listing path
                     criteria made site paths
    6  clean         drop Slate inline elements with no text (they crash the editor)
    7  check         counts, parent order, blobs, Slate validity
       split         scripts/split_transformed_json.py (only with --split)

Fixes carried here so scripts/ needs no changes:

* Alignment: image blocks hold /resolveuid/<uid> while production pages use
  filenames; the UID is mapped to the image's filename before matching.
* Subsite item: its parent is the target's language folder (LRF) by UID, not
  "Plone Site". With "Plone Site" the importer looks in the site root and
  creates a second /<site>/epanet. The UID is needed when importing at the
  subsite itself, because it is a navigation root and the importer ignores
  parents found by path outside it.
* Empty inline Slate nodes, e.g. <strong><img/></strong> after the image is
  extracted, are removed.
* review_state: epanet_migrate.py drops it, but the importer only updates the
  catalog (the navigation lists published items only) and permissions when it
  runs a transition to review_state. It is put back from the export, except
  for published items without an effective date: publishing would set that to
  now. An existing effective date is never changed.
* Listing blocks: epanet_migrate.py writes path criteria as URLs, which match
  nothing (absolutePath prefixes the portal path); they become site paths.

The runner only reads the target (to look up the parent UID); it never writes.

Usage (from the repository root, converter running on :8000):

    python3 scripts/build_epanet_import.py --input fresh-test/epanet.json
    # -> content-exports/epanet-import/epanet-import.json

    # another output directory, a known parent UID, and chunks
    python3 scripts/build_epanet_import.py --input fresh-test/epanet.json \\
        --output-dir fresh-test/run --parent-uid 088f27bb8dc246e593bfc85cf941fa71 --split
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("build_epanet_import")

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
DEFAULT_OUT = REPO / "content-exports" / "epanet-import"
DEFAULT_TARGET_ROOT = "http://localhost:8080/Plone/en/epanet"
DEFAULT_CONVERTER_URL = "http://localhost:8000/toblocks"
FRONT_PAGE_ID = "front-page"
SUBSITE_TYPE = "Subsite"
LANGUAGE_FOLDER_TYPE = "LRF"
RESOLVEUID = re.compile(r"resolveuid/([0-9a-f]{32})")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--input", type=Path, required=True,
                   help="Raw collective.exportimport export, e.g. fresh-test/epanet.json")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT,
                   help="Where all files are written (default: %(default)s)")
    p.add_argument("--target-root", default=DEFAULT_TARGET_ROOT,
                   help="Target subsite URL (default: %(default)s)")
    p.add_argument("--parent-uid", default=None,
                   help="UID of the subsite's parent (e.g. /en) on the target; "
                        "looked up from the target when omitted")
    p.add_argument("--converter-url", default=DEFAULT_CONVERTER_URL,
                   help="Blocks converter endpoint (default: %(default)s)")
    p.add_argument("--skip-validate", action="store_true", help="Skip step 1")
    p.add_argument("--skip-align", action="store_true",
                   help="Skip step 3 (it reads the production EPANET site)")
    p.add_argument("--split", action="store_true",
                   help="Also split epanet-import.json into chunks")
    return p.parse_args()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def run_script(label: str, script: str, *script_args: Any) -> None:
    cmd = [sys.executable, str(SCRIPTS / script), *[str(a) for a in script_args]]
    logger.info("=== %s", label)
    logger.info("    %s", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=REPO)


def load(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def dump(items: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(items, handle, ensure_ascii=False)


def converter_is_up(url: str) -> bool:
    request = urllib.request.Request(
        url,
        data=json.dumps({"html": "<p>ping</p>"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def lookup_uid(url: str) -> str | None:
    """Read an object's UID from the target's REST API (anonymous GET)."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response).get("UID")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def blocks_in(value: Any) -> Iterator[dict[str, Any]]:
    """Every block, including blocks nested in columns, groups and grids."""
    if isinstance(value, dict):
        if value.get("@type"):
            yield value
        for child in value.values():
            yield from blocks_in(child)
    elif isinstance(value, list):
        for child in value:
            yield from blocks_in(child)


def has_text(node: Any) -> bool:
    return isinstance(node, dict) and (
        isinstance(node.get("text"), str)
        or any(has_text(child) for child in node.get("children", []))
    )


def slate_values(items: list[dict[str, Any]]) -> Iterator[tuple[dict, list]]:
    for item in items:
        for block in blocks_in(item.get("blocks") or {}):
            if block.get("@type") == "slate" and isinstance(block.get("value"), list):
                yield item, block["value"]


def parent_id(item: dict[str, Any]) -> str:
    return (item.get("parent") or {}).get("@id", "").rstrip("/")


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #


def align_images(transformed: Path, fixed: Path) -> None:
    """Step 3: run the alignment script in JSON mode with resolveuid matching."""
    logger.info("=== 3/7 fix image alignment")
    sys.path.insert(0, str(SCRIPTS))
    import fix_news_image_alignment as align  # noqa: E402

    uid_names = {
        item["UID"]: align.basename(item.get("@id", ""))
        for item in load(transformed)
        if isinstance(item, dict) and item.get("UID")
    }
    plain_basename = align.basename

    def basename(url: str) -> str:
        match = RESOLVEUID.search(url)
        if match and match.group(1) in uid_names:
            return uid_names[match.group(1)]
        return plain_basename(url)

    # apply_alignments() names block images via basename(); production <img>
    # src never contains resolveuid, so it is unaffected.
    align.basename = basename

    argv = sys.argv
    sys.argv = ["fix_news_image_alignment.py",
                "--input-json", str(transformed), "--output-json", str(fixed)]
    try:
        align_args = align.parse_args()
    finally:
        sys.argv = argv
    if align.run_json(align_args) != 0:
        raise RuntimeError("image alignment failed")


def combine(items: list[dict[str, Any]], target_root: str,
            parent_uid: str | None) -> list[dict[str, Any]]:
    """Step 5: the Subsite carries the front-page blocks; front-page is dropped."""
    logger.info("=== 5/7 put the front-page blocks on the subsite")
    front_page = next(
        (item for item in items
         if item.get("id") == FRONT_PAGE_ID and parent_id(item) == target_root),
        None,
    )
    if front_page is None:
        raise RuntimeError(f"no {FRONT_PAGE_ID} item directly under {target_root}")
    if any(parent_id(item) == front_page["@id"].rstrip("/") for item in items):
        raise RuntimeError(f"{FRONT_PAGE_ID} has children; it cannot be dropped")
    if front_page.get("UID") and any(
        front_page["UID"] in json.dumps(item.get("blocks") or {})
        for item in items if item is not front_page
    ):
        logger.warning("Other pages link to %s; those links will not resolve",
                       FRONT_PAGE_ID)

    parent = {"@id": target_root.rsplit("/", 1)[0], "@type": LANGUAGE_FOLDER_TYPE}
    if parent_uid:
        parent["UID"] = parent_uid
    subsite = {
        "@id": target_root,
        "id": target_root.rsplit("/", 1)[-1],
        "@type": SUBSITE_TYPE,
        "parent": parent,
        "blocks": front_page.get("blocks") or {},
        "blocks_layout": front_page.get("blocks_layout") or {"items": []},
    }
    logger.info("Subsite item: parent %s, %d block(s)",
                parent, len(subsite["blocks_layout"]["items"]))
    return [subsite] + [item for item in items if item is not front_page]


def restore_review_state(items: list[dict[str, Any]], source: list[dict[str, Any]]) -> int:
    """Put back the export's review_state; return how many items got one.

    The importer's transition to it updates the catalog and permissions. A
    published item without an effective date stays without: publishing would
    set its effective date to now.
    """
    logger.info("=== 5/7 restore review_state")
    states = {i["UID"]: i.get("review_state") for i in source if i.get("UID")}
    restored = 0
    for item in items:
        state = states.get(item.get("UID"))
        if not state or (state == "published" and not item.get("effective")):
            continue
        item["review_state"] = state
        restored += 1
    logger.info("review_state on %d item(s)", restored)
    return restored


def fix_listing_paths(items: list[dict[str, Any]], target_root: str) -> int:
    """Make URL path criteria of listing blocks site paths; return how many."""
    logger.info("=== 5/7 listing path criteria")
    target_path = urlparse(target_root).path.rstrip("/")
    fixed = 0
    for item in items:
        for block in blocks_in(item.get("blocks") or {}):
            if block.get("@type") != "listing":
                continue
            for criterion in (block.get("querystring") or {}).get("query", []):
                value = criterion.get("v")
                if criterion.get("i") == "path" and isinstance(value, str) and "://" in value:
                    path = urlparse(value).path
                    if path.startswith(target_path):
                        criterion["v"] = path
                        fixed += 1
    logger.info("Fixed %d path criterion(s)", fixed)
    return fixed


def clean_slate(items: list[dict[str, Any]]) -> int:
    """Step 6: remove inline elements without text; return how many."""
    logger.info("=== 6/7 remove empty inline Slate elements")
    removed = 0

    def clean(children: list) -> list:
        nonlocal removed
        kept = []
        for child in children:
            if isinstance(child, dict) and "text" not in child:
                if not has_text(child):
                    removed += 1
                    continue
                child["children"] = clean(child.get("children", []))
            kept.append(child)
        return kept or [{"text": ""}]

    for item, value in slate_values(items):
        before = removed
        for top in value:
            if isinstance(top, dict) and "children" in top:
                top["children"] = clean(top["children"])
        if removed > before:
            logger.info("  cleaned %s", item["@id"])
    logger.info("Removed %d empty inline element(s)", removed)
    return removed


def check(items: list[dict[str, Any]], target_root: str) -> list[str]:
    """Step 7: return a list of problems (empty when the file looks right)."""
    logger.info("=== 7/7 check epanet-import.json")
    problems = []

    logger.info("Items: %d %s", len(items), dict(Counter(i.get("@type") for i in items)))

    if items[0].get("@type") != SUBSITE_TYPE or items[0]["@id"] != target_root:
        problems.append("the first item is not the Subsite")
    if any(item.get("id") == FRONT_PAGE_ID and parent_id(item) == target_root
           for item in items[1:]):
        problems.append(f"{FRONT_PAGE_ID} is still present")

    outside = [i["@id"] for i in items if not i["@id"].startswith(target_root)]
    if outside:
        problems.append(f"{len(outside)} item(s) outside {target_root}, e.g. {outside[0]}")

    position = {item["@id"].rstrip("/"): n for n, item in enumerate(items)}
    late = [item["@id"] for n, item in enumerate(items)
            if position.get(parent_id(item), -1) > n]
    if late:
        problems.append(f"{len(late)} item(s) come before their parent, e.g. {late[0]}")

    no_blob = [item["@id"] for item in items
               if item.get("@type") in ("File", "Image")
               and not (item.get("file") or item.get("image") or {}).get("data")]
    if no_blob:
        problems.append(f"{len(no_blob)} File/Image item(s) without data, e.g. {no_blob[0]}")

    def textless(nodes: list) -> int:
        bad = 0
        for node in nodes:
            if isinstance(node, dict) and "text" not in node:
                if not node.get("children") or not has_text(node):
                    bad += 1
                else:
                    bad += textless(node["children"])
        return bad

    bad_slate = sum(textless(value) for _, value in slate_values(items))
    if bad_slate:
        problems.append(f"{bad_slate} Slate element(s) without text")

    block_types = Counter(block["@type"] for item in items
                          for block in blocks_in(item.get("blocks") or {}))
    aligned = sum(1 for item in items
                  for block in (item.get("blocks") or {}).values()
                  if block.get("@type") == "image" and block.get("align") in ("left", "right"))
    logger.info("Aligned images: %d, teaserGrid: %d, listing: %d, columnsBlock: %d",
                aligned, block_types["teaserGrid"], block_types["listing"],
                block_types["columnsBlock"])
    return problems


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main() -> int:
    args = parse_args()
    source = args.input.resolve()
    out = args.output_dir.resolve()
    root = args.target_root.rstrip("/")

    if not source.exists():
        logger.error("Input not found: %s", source)
        return 1
    if not converter_is_up(args.converter_url):
        logger.error("The blocks converter is not reachable at %s; start it first:",
                     args.converter_url)
        logger.error("  cd ../volto-blocks-converter && "
                     ".venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000")
        return 1

    parent_uid = args.parent_uid or lookup_uid(root.rsplit("/", 1)[0])
    out.mkdir(parents=True, exist_ok=True)

    transformed = out / "epanet-transformed.json"
    fixed = out / "epanet-transformed-fixed.json"
    teasers = out / "epanet-transformed-fixed-teasers.json"
    result = out / "epanet-import.json"

    try:
        if not args.skip_validate:
            run_script("1/7 validate the export", "validate_export.py",
                       "--input", source, "--report", out / "VALIDATION_REPORT.md")
        run_script("2/7 transform the export", "epanet_migrate.py",
                   "--input", source, "--output", transformed,
                   "--collections-output", out / "collections.json",
                   "--report", out / "TRANSFORM_REPORT.md",
                   "--target-root", root, "--converter-url", args.converter_url)
        if args.skip_align:
            logger.info("=== 3/7 fix image alignment: skipped")
            fixed = transformed
        else:
            align_images(transformed, fixed)
        run_script("4/7 build the member teaser grid", "build_teaser_grid.py",
                   "--input", fixed, "--output", teasers)

        items = combine(load(teasers), root, parent_uid)
        restore_review_state(items, load(source))
        fix_listing_paths(items, root)
        clean_slate(items)
        problems = check(items, root)
        dump(items, result)
        logger.info("Wrote %s (%d items)", result, len(items))

        if args.split:
            run_script("split epanet-import.json", "split_transformed_json.py",
                       result, "--output-dir", out / "epanet-import-chunks")
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        logger.error("Pipeline stopped: %s", exc)
        return 1

    logger.info("")
    logger.info("=" * 72)
    if problems:
        for problem in problems:
            logger.error("CHECK FAILED: %s", problem)
        logger.info("=" * 72)
        return 1
    logger.info("All checks passed. Import:")
    logger.info("  %s", result)
    if parent_uid:
        logger.info("  at %s/@@import_content", root)
    else:
        logger.warning("  The parent UID could not be read from the target; import at the")
        logger.warning("  SITE ROOT (%s/@@import_content),", root.split("/en/")[0])
        logger.warning("  or re-run with --parent-uid <UID of %s>.", root.rsplit("/", 1)[0])
    logger.info("  with 'Update existing content' (handle_existing_content=2).")
    logger.info("Afterwards run updateRoleMappings and reindex allowedRolesAndUsers.")
    logger.info("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
