#!/usr/bin/env python3
"""Align image blocks in epanet News Items to match production.

Images in Classic Plone are floated via either an inline `style="float: ..."`
or an `image-left` / `image-right` class. The Volto image block stores that as
its `align` property.

Two modes:

* API mode (default): search a running Plone, PATCH the matched image blocks.
* JSON mode (--input-json): read a collective.exportimport JSON export, set the
  `align` on its image blocks and write a new, re-importable JSON file.

The production page is always the source of truth for the alignment.

Usage:
    python scripts/fix_news_image_alignment.py --dry-run
    python scripts/fix_news_image_alignment.py
    python scripts/fix_news_image_alignment.py --input-json news.json --output-json news-fixed.json
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("fix_news_image_alignment")

DEFAULT_LOCAL_BASE = "http://localhost:8080/Plone"
DEFAULT_LOCAL_PATH = "/en/epanet/reports-letters/plenary-meetings"
DEFAULT_PROD_BASE = "https://epanet.eea.europa.eu"
DEFAULT_USER = "admin"
DEFAULT_PASS = "admin"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-base", default=DEFAULT_LOCAL_BASE)
    parser.add_argument("--local-path", default=DEFAULT_LOCAL_PATH)
    parser.add_argument("--prod-base", default=DEFAULT_PROD_BASE)
    parser.add_argument(
        "--prod-path",
        default=None,
        help="Path on the production site (default: derived from --local-path)",
    )
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASS)
    parser.add_argument("--input-json", default=None, help="Export JSON (enables JSON mode)")
    parser.add_argument("--output-json", default=None, help="Where to write the fixed JSON")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def auth_header(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def http_json(
    url: str,
    method: str = "GET",
    payload: Any = None,
    headers: dict[str, str] | None = None,
) -> Any:
    headers = dict(headers or {})
    headers.setdefault("Accept", "application/json")
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
        return json.loads(body.decode()) if body else {}


def http_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def basename(url: str) -> str:
    """'.../file.jpg/@@images/x.jpeg' -> 'file.jpg'"""
    return url.split("/@@images")[0].rstrip("/").rsplit("/", 1)[-1]


def block_image_name(url: str, uid_names: dict[str, str] | None) -> str:
    """Filename of a block's image; '/resolveuid/<uid>' goes through uid_names."""
    match = re.search(r"resolveuid/([0-9a-f]{32})", url)
    if match and uid_names:
        return uid_names.get(match.group(1), basename(url))
    return basename(url)


def path_without_site_prefix(path: str) -> str:
    for prefix in ("/en/epanet", "/epanet"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def production_url(prod_base: str, prod_path: str, slug: str) -> str:
    return prod_base.rstrip("/") + prod_path.rstrip("/") + "/" + slug


def effective_float(tag: str) -> str:
    """Inline style wins over the class, matching how the browser renders it."""
    style = re.search(r'style="([^"]*)"', tag)
    style = (style.group(1) if style else "").lower()
    if "float: left" in style:
        return "left"
    if "float: right" in style:
        return "right"

    cls = re.search(r'class="([^"]*)"', tag)
    cls = (cls.group(1) if cls else "").lower()
    if "image-left" in cls:
        return "left"
    if "image-right" in cls:
        return "right"
    return ""


def production_alignments(html: str) -> dict[str, str]:
    """Map image filename -> effective float, from a production page."""
    result: dict[str, str] = {}
    for match in re.finditer(r"<img[^>]*>", html, re.IGNORECASE):
        tag = match.group(0)
        if "matomo" in tag or "site-logo" in tag:
            continue
        src = re.search(r'src="([^"]+)"', tag)
        if not src:
            continue
        align = effective_float(tag)
        if align:
            result.setdefault(basename(src.group(1)), align)
    return result


def image_block_ids(blocks: dict[str, Any], layout: list[str]) -> list[str]:
    return [uid for uid in layout if blocks.get(uid, {}).get("@type") == "image"]


def apply_alignments(
    blocks: dict[str, Any],
    layout: list[str],
    prod_align: dict[str, str],
    uid_names: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    """Return (new_blocks, [(filename, old_align, new_align), ...])."""
    new_blocks = dict(blocks)
    changes: list[tuple[str, str, str]] = []
    for uid in image_block_ids(blocks, layout):
        block = blocks[uid]
        name = block_image_name(block.get("url", ""), uid_names)
        align = prod_align.get(name)
        if not align:
            logger.warning("  no production float for %s (%s)", uid, name)
            continue
        if block.get("align") == align:
            continue
        new_blocks[uid] = {**block, "align": align}
        changes.append((name, block.get("align"), align))
    return new_blocks, changes


def fetch_production_alignments(
    prod_base: str, prod_path: str, slug: str
) -> dict[str, str] | None:
    url = production_url(prod_base, prod_path, slug)
    try:
        return production_alignments(http_text(url))
    except urllib.error.HTTPError as exc:
        logger.warning("  skipping %s: production page %s", slug, exc.code)
        return None


# --------------------------------------------------------------------------- #
# API mode
# --------------------------------------------------------------------------- #


def run_api(args: argparse.Namespace) -> int:
    base = args.local_base.rstrip("/")
    headers = {"Authorization": auth_header(args.user, args.password)}
    prod_path = args.prod_path or path_without_site_prefix(args.local_path)

    # the catalog stores the physical path (e.g. /Plone/en/epanet/...)
    physical_path = urlparse(base).path.rstrip("/") + args.local_path
    search = http_json(
        f"{base}/++api++/@search?path.query={physical_path}&path.depth=-1"
        f"&portal_type=News%20Item&b_size=1000&sort_on=path",
        headers=headers,
    )
    items = search.get("items", [])
    logger.info("Found %d News Item(s) under %s", len(items), args.local_path)

    changed_items = 0
    changed_images = 0

    for item in items:
        url = item["@id"].replace("http://localhost:3000", base + "/++api++")
        data = http_json(url, headers=headers)
        blocks = data.get("blocks", {}) or {}
        layout = (data.get("blocks_layout", {}) or {}).get("items", [])
        if not image_block_ids(blocks, layout):
            continue

        slug = item["@id"].rstrip("/").rsplit("/", 1)[-1]
        prod_align = fetch_production_alignments(args.prod_base, prod_path, slug)
        if prod_align is None:
            continue

        new_blocks, changes = apply_alignments(blocks, layout, prod_align)
        if not changes:
            continue

        for name, old, new in changes:
            logger.info(
                "%s %s: %s align %r -> %r",
                "Would update" if args.dry_run else "Updating",
                slug,
                name,
                old,
                new,
            )
        changed_items += 1
        changed_images += len(changes)

        if args.dry_run:
            continue
        http_json(
            url,
            method="PATCH",
            payload={"blocks": new_blocks, "blocks_layout": {"items": layout}},
            headers=headers,
        )

    logger.info(
        "Done. %d image(s) across %d item(s)%s.",
        changed_images,
        changed_items,
        " would change" if args.dry_run else " updated",
    )
    return 0


# --------------------------------------------------------------------------- #
# JSON mode
# --------------------------------------------------------------------------- #


def run_json(args: argparse.Namespace) -> int:
    prod_path = args.prod_path or path_without_site_prefix(args.local_path)
    output = args.output_json
    if not output:
        output = re.sub(r"\.json$", "", args.input_json) + "-fixed.json"

    with open(args.input_json, encoding="utf-8") as handle:
        items = json.load(handle)
    logger.info("Read %d item(s) from %s", len(items), args.input_json)

    # Image blocks reference images as /resolveuid/<uid>; production pages use
    # filenames. Map each exported item's UID to its filename to match them.
    uid_names = {
        item["UID"]: basename(item.get("@id", ""))
        for item in items
        if isinstance(item, dict) and item.get("UID")
    }

    changed_items = 0
    changed_images = 0
    result: list[Any] = []

    for item in items:
        if not isinstance(item, dict) or not item.get("blocks"):
            result.append(item)
            continue

        layout = (item.get("blocks_layout", {}) or {}).get("items", [])
        if not image_block_ids(item["blocks"], layout):
            result.append(item)
            continue

        slug = item.get("id") or item.get("@id", "").rstrip("/").rsplit("/", 1)[-1]
        prod_align = fetch_production_alignments(args.prod_base, prod_path, slug)
        if prod_align is None:
            result.append(item)
            continue

        new_blocks, changes = apply_alignments(
            item["blocks"], layout, prod_align, uid_names
        )
        if not changes:
            result.append(item)
            continue

        for name, old, new in changes:
            logger.info("  %s: %s align %r -> %r", slug, name, old, new)
        changed_items += 1
        changed_images += len(changes)
        cleaned = dict(item)
        cleaned["blocks"] = new_blocks
        result.append(cleaned)

    logger.info(
        "Done. %d image(s) across %d item(s)%s.",
        changed_images,
        changed_items,
        " would change" if args.dry_run else " updated",
    )

    if args.dry_run:
        return 0
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    logger.info("Wrote %s", output)
    return 0


def main() -> int:
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    if args.input_json:
        return run_json(args)
    return run_api(args)


if __name__ == "__main__":
    sys.exit(main())
