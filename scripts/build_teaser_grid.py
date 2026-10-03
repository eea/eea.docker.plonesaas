#!/usr/bin/env python3
"""Replace the /en/epanet/our-group member table with a group of teaserGrid blocks.

The collective.exportimport transform converts the member table into a
`slateTable`. This script reads that transformed JSON, extracts one entry per
table cell (image + agency link + the cell's visible name) and replaces the
`slateTable` with a `group` block containing `teaserGrid` blocks of 4 columns.

Each teaser:
  * targets the Image content item (resolveuid)
  * title      = the cell's visible text (the country/agency name)
  * external_link = the agency URL from the cell's link

Usage:
    python scripts/build_teaser_grid.py --dry-run
    python scripts/build_teaser_grid.py --input fresh-test/epanet-transformed.json
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("build_teaser_grid")

DEFAULT_INPUT = (
    Path(__file__).parent.parent / "content-exports" / "epanet-transformed.json"
)
DEFAULT_PAGE = "/en/epanet/our-group"
COLUMNS = 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--page",
        default=DEFAULT_PAGE,
        help="Site path of the page holding the member table (default: %(default)s)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def make_uid() -> str:
    return str(uuid.uuid4())


def item_path(item: dict[str, Any]) -> str:
    raw = item.get("@id", "")
    return urlparse(raw).path or raw


# --------------------------------------------------------------------------- #
# slateTable walking
# --------------------------------------------------------------------------- #


def first_text(node: Any) -> str | None:
    """First non-empty text in document order (the cell's visible name)."""
    if isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        for child in node.get("children", []):
            found = first_text(child)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = first_text(child)
            if found:
                return found
    return None


def find_image(node: Any, link: str | None = None) -> tuple[Any, str | None] | None:
    """Return (img_node, enclosing_link_url) for the first img in the tree."""
    if isinstance(node, dict):
        if node.get("type") == "link":
            link = (node.get("data") or {}).get("url") or link
        if node.get("type") == "img":
            return node, link
        for child in node.get("children", []):
            found = find_image(child, link)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = find_image(child, link)
            if found:
                return found
    return None


def image_uid(url: str) -> str | None:
    match = re.search(r"resolveuid/([0-9a-f]+)", url or "")
    return match.group(1) if match else None


# --------------------------------------------------------------------------- #
# block builders
# --------------------------------------------------------------------------- #


def build_teaser(title: str, uid: str, external: str | None) -> dict[str, Any]:
    return {
        "@type": "teaser",
        "id": make_uid(),
        "href": [{"@id": f"/resolveuid/{uid}", "image_field": "image"}],
        "title": title,
        "external_link": external or "",
        "itemModel": {
            "@type": "imageOnBottom",
            "hasLink": True,
            # logos keep their aspect ratio instead of being cropped
            "styles": {"objectFit": "contain"},
        },
    }


def build_teaser_grid(teasers: list[dict[str, Any]]) -> dict[str, Any]:
    # The teaserGrid View reads `data.columns` and maps each column's `id`.
    return {"@type": "teaserGrid", "columns": teasers}


def build_group(grids: list[dict[str, Any]]) -> dict[str, Any]:
    blocks: dict[str, Any] = {}
    layout: list[str] = []
    for grid in grids:
        uid = make_uid()
        blocks[uid] = grid
        layout.append(uid)
    return {
        "@type": "group",
        "data": {"blocks": blocks, "blocks_layout": {"items": layout}},
    }


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #


def resolve_link(url: str, uid_to_url: dict[str, str]) -> str:
    """Resolve a `resolveuid/<uid>` link to the target item's URL.

    Internal links stay as resolveuid in the export; the teaser stores the
    resolved URL (e.g. .../our-group/website-norwegian-epa) instead.
    """
    match = re.search(r"resolveuid/([0-9a-f]+)", url or "")
    if not match:
        return url
    target = uid_to_url.get(match.group(1))
    if not target:
        return url
    # Keep the site-relative path (drop the origin and any site root such as
    # /admin or /Plone) so the link works for anonymous visitors.
    path = urlparse(target).path or target
    index = path.find("/en/")
    return path[index:] if index != -1 else target


def entries_from_table(
    table: dict[str, Any], uid_to_url: dict[str, str]
) -> list[dict[str, Any]]:
    """One entry per cell holding an image: {title, uid, external}."""
    entries: list[dict[str, Any]] = []
    rows = (table.get("table") or {}).get("rows", [])
    for row in rows:
        for cell in row.get("cells", []):
            value = cell.get("value", [])
            found = find_image(value)
            if not found:
                continue
            img, link = found
            uid = image_uid(img.get("url", ""))
            if not uid:
                logger.warning("  cell image without resolveuid, skipped")
                continue
            title = first_text(value) or img.get("title") or ""
            entries.append(
                {"title": title, "uid": uid, "external": resolve_link(link or "", uid_to_url)}
            )
    return entries


def chunk(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def main() -> int:
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    output = args.output
    if output is None:
        output = args.input.with_name(args.input.stem + "-teasers.json")

    with args.input.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    logger.info("Read %d item(s) from %s", len(data), args.input)

    page = None
    for item in data:
        if isinstance(item, dict) and item_path(item).endswith(args.page):
            page = item
            break
    if page is None:
        logger.error("No item found for path %s", args.page)
        return 1
    logger.info("Found page %s", item_path(page))

    blocks = page.get("blocks") or {}
    layout = (page.get("blocks_layout") or {}).get("items", [])
    table_id = next(
        (uid for uid in layout if blocks.get(uid, {}).get("@type") == "slateTable"),
        None,
    )
    if table_id is None:
        logger.error("No slateTable block on %s", args.page)
        return 1

    uid_to_url = {
        item["UID"]: item.get("@id", "")
        for item in data
        if isinstance(item, dict) and item.get("UID")
    }
    entries = entries_from_table(blocks[table_id], uid_to_url)
    logger.info("Extracted %d entries from the table", len(entries))
    missing_link = [e for e in entries if not e["external"]]
    if missing_link:
        logger.warning("  %d entry(ies) without an external link", len(missing_link))
    missing_title = [e for e in entries if not e["title"]]
    if missing_title:
        logger.warning("  %d entry(ies) without a title", len(missing_title))

    teasers = [build_teaser(e["title"], e["uid"], e["external"]) for e in entries]
    grids = [build_teaser_grid(group) for group in chunk(teasers, COLUMNS)]
    logger.info(
        "Built %d teaserGrid block(s) of up to %d columns (%d teasers)",
        len(grids),
        COLUMNS,
        len(teasers),
    )

    group = build_group(grids)
    new_blocks = dict(blocks)
    new_blocks[table_id] = group

    if args.dry_run:
        logger.info(
            "Dry run: would replace slateTable %s with a group of %d grid(s)",
            table_id,
            len(grids),
        )
        return 0

    payload = {"blocks": new_blocks, "blocks_layout": {"items": layout}}
    transformed = []
    for item in data:
        if item is page:
            merged = dict(item)
            merged["blocks"] = payload["blocks"]
            merged["blocks_layout"] = payload["blocks_layout"]
            transformed.append(merged)
        else:
            transformed.append(item)

    with output.open("w", encoding="utf-8") as handle:
        json.dump(transformed, handle, ensure_ascii=False, indent=2)
    logger.info("Wrote %s", output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
