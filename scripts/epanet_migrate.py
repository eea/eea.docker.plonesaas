#!/usr/bin/env python3
"""Transform an EPANET collective.exportimport JSON export into import-ready Volto blocks.

This script performs the offline transformation described in EPANET_MIGRATION_PLAN.md:
  - type mapping (Folder -> Document)
  - language normalization ('en-gb' / '' -> 'en')
  - path rewriting so the content lands under the target subsite
  - HTML-to-Volto-blocks conversion via eea-volto-blocks-converter
  - collection extraction (rebuild manually as listing blocks)

Usage:
    python scripts/epanet_migrate.py
    python scripts/epanet_migrate.py --target-root https://demo-www.eea.europa.eu/en/epanet

Outputs:
    content-exports/epanet-transformed.json   import-ready content
    content-exports/collections.json          collections for manual rebuild
    content-exports/TRANSFORM_REPORT.md       summary and any errors
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import urllib.request

logger = logging.getLogger("epanet_migrate")

# Source site root as it appears in the export file.
OLD_ROOT = "https://epanet.eea.europa.eu"

# Default target for local eea-website-backend stack.
DEFAULT_TARGET_ROOT = "http://localhost:8080/Plone/en/epanet"
DEFAULT_TEMPLATE = (
    Path(__file__).parent.parent / "content-exports" / "press-release-template.json"
)

# collective.volto.subsites type name.
SUBSITE_TYPE = "Subsite"

# Content types that should receive Volto blocks.
BLOCKS_TYPES = {"Document", "News Item"}

# Private items of these types are still included, because other content links
# to them. Link items only carry an external remoteUrl, so including them is
# safe and keeps internal resolveuid links resolvable.
PRIVATE_TYPE_EXCEPTIONS = {"Link"}

# Fields that are safe / useful to keep. Everything else is dropped.
KEEP_FIELDS = {
    "@id",
    "@type",
    "UID",
    "allow_discussion",
    "changeNote",
    "contributors",
    "created",
    "creators",
    "description",
    "effective",
    "exclude_from_nav",
    "expires",
    "id",
    "image",
    "image_caption",
    "language",
    "modified",
    "parent",
    "remoteUrl",
    "rights",
    "subjects",
    "table_of_contents",
    "text",
    "title",
    "workflow_history",
    "file",
}

# Serializer-only noise or source-only fields that should never be imported.
DROP_FIELDS = {
    "is_folderish",
    "layout",
    "lock",
    "nextPreviousEnabled",
    "review_state",
    "type_title",
    "version",
    "versioning_enabled",
    "working_copy",
    "working_copy_of",
}

# Map old classic types to target Dexterity types.
TYPE_MAP = {
    "Folder": "Document",
    "Collection": "Document",
    "Document": "Document",
    "News Item": "News Item",
    "File": "File",
    "Image": "Image",
    "Link": "Link",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).parent.parent / "content-exports" / "epanet.json",
        help="Path to the raw export JSON (default: content-exports/epanet.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent.parent / "content-exports" / "epanet-transformed.json",
        help="Path for the transformed import JSON",
    )
    parser.add_argument(
        "--collections-output",
        type=Path,
        default=Path(__file__).parent.parent / "content-exports" / "collections.json",
        help="Path for the collections reference JSON (informational)",
    )
    parser.add_argument(
        "--report-output",
        type=Path,
        default=Path(__file__).parent.parent / "content-exports" / "TRANSFORM_REPORT.md",
        help="Path for the Markdown report",
    )
    parser.add_argument(
        "--target-root",
        default=DEFAULT_TARGET_ROOT,
        help="URL of the target subsite root (e.g. http://localhost:8080/Plone/en/epanet)",
    )
    parser.add_argument(
        "--old-root",
        default=OLD_ROOT,
        help="Source site root URL as found in the export",
    )
    parser.add_argument(
        "--converter-url",
        default="http://localhost:8000/toblocks",
        help="URL of the eea-volto-blocks-converter /toblocks endpoint",
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=DEFAULT_TEMPLATE,
        help="News Item default-blocks template (default: content-exports/press-release-template.json)",
    )
    parser.add_argument(
        "--include-private",
        action="store_true",
        help="Include items whose review_state is 'private' (default: skip them)",
    )
    parser.add_argument(
        "--skip-collections",
        action="store_true",
        help="Skip collections entirely instead of extracting them",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    return parser.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def relative_path(url: str, root: str) -> str:
    """Return the path of url relative to root, with leading slash."""
    root_parsed = urlparse(root.rstrip("/") + "/")
    url_parsed = urlparse(url)
    root_path = root_parsed.path.rstrip("/")
    url_path = url_parsed.path
    if url_path.startswith(root_path + "/"):
        return url_path[len(root_path):]
    if url_path == root_path:
        return "/"
    return url_path


def target_url(old_url: str, old_root: str, target_root: str) -> str:
    """Rewrite an old EPANET URL to the target subsite URL."""
    rel = relative_path(old_url, old_root)
    return urljoin(target_root.rstrip("/") + "/", rel.lstrip("/"))


def is_top_level(item: dict[str, Any], old_root: str) -> bool:
    """True if the item's parent is the old Plone site root."""
    parent = item.get("parent", {})
    parent_id = parent.get("@id", "")
    return (
        parent.get("@type") == "Plone Site"
        or parent_id.rstrip("/") == old_root.rstrip("/")
    )


def post_json(url: str, payload: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    """POST JSON data using urllib and return the parsed JSON response."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def html_to_blocks(html: str, converter_url: str) -> tuple[dict[str, Any], list[str]]:
    """Call the blocks converter and return (blocks_dict, blocks_layout_items)."""
    if not html or not html.strip():
        return {}, []

    payload = post_json(converter_url, {"html": html})
    blocks_list = payload.get("data", [])

    blocks: dict[str, Any] = {}
    layout: list[str] = []
    for entry in blocks_list:
        # Converter returns [[uid, block], ...]
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            uid, block = entry
        elif isinstance(entry, dict) and "uid" in entry and "block" in entry:
            uid, block = entry["uid"], entry["block"]
        else:
            logger.warning("Unexpected converter entry shape: %r", entry)
            continue
        blocks[uid] = block
        layout.append(uid)
    return blocks, layout


def template_block(template: dict[str, Any], block_type: str) -> dict[str, Any]:
    """Deep-copy the template block of the given @type."""
    blocks = template.get("blocks", {})
    for uid in template.get("blocks_layout", {}).get("items", []):
        if blocks.get(uid, {}).get("@type") == block_type:
            return json.loads(json.dumps(blocks[uid]))
    raise KeyError(f"template has no {block_type} block")


def news_item_default_blocks(template: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the DX default header blocks for a News Item.

    layoutSettings, description and dividerBlock, in that order.
    """
    return [
        template_block(template, "layoutSettings"),
        template_block(template, "description"),
        template_block(template, "dividerBlock"),
    ]


def collection_listing_block(
    item: dict[str, Any], target_root: str
) -> dict[str, Any]:
    """Build a listing block from a Collection's saved query.

    The Collection's own query is reused, so the listing shows the same items
    as the original page. A path criterion is rewritten to the target root.
    """
    query = json.loads(json.dumps(item.get("query") or []))
    for criterion in query:
        value = criterion.get("v")
        if criterion.get("i") != "path" or not isinstance(value, str):
            continue
        # '/epanet/reports-letters' -> '<target_root>/reports-letters'
        rel = value
        for prefix in ("/epanet", "/en/epanet"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
                break
        criterion["v"] = target_root.rstrip("/") + "/" + rel.lstrip("/")

    listing: dict[str, Any] = {
        "@type": "listing",
        "querystring": {
            "query": query,
            "sort_on": item.get("sort_on") or "effective",
            "sort_order": "descending" if item.get("sort_reversed") else "ascending",
            "limit": str(item.get("limit") or 1000),
            "b_size": "20",
        },
        "variation": "default",
        "itemModel": {"@type": "card", "hasLink": True},
    }
    return listing


def build_blocks(
    item: dict[str, Any],
    converter_url: str,
    template: dict[str, Any] | None = None,
    target_root: str = DEFAULT_TARGET_ROOT,
) -> tuple[dict[str, Any], list[str]]:
    """Build the complete Volto blocks structure for an item."""
    blocks: dict[str, Any] = {}
    layout: list[str] = []

    # News Items get the DX default header blocks.
    use_defaults = item.get("@type") == "News Item" and bool(template)

    # Title block (always first).
    title_uid = str(uuid.uuid4())
    title_block: dict[str, Any] = {"@type": "title"}
    if item.get("@type") == "News Item":
        title_block["hideContentType"] = True
    blocks[title_uid] = title_block
    layout.append(title_uid)

    if use_defaults:
        for block in news_item_default_blocks(template):
            uid = str(uuid.uuid4())
            blocks[uid] = block
            layout.append(uid)
    else:
        # Description block (only when there is a description).
        description = (item.get("description") or "").strip()
        if description:
            desc_uid = str(uuid.uuid4())
            blocks[desc_uid] = {"@type": "description"}
            layout.append(desc_uid)

    # Body blocks from the rich text/HTML field.
    text_field = item.get("text")
    html = ""
    if isinstance(text_field, dict):
        html = text_field.get("data", "")
    elif isinstance(text_field, str):
        html = text_field

    if html and html.strip():
        body_blocks, body_layout = html_to_blocks(html, converter_url)
        blocks.update(body_blocks)
        layout.extend(body_layout)

    # Collections become Documents whose body ends with a listing block built
    # from the original saved query.
    if item.get("@type") == "Collection":
        collection_listing = collection_listing_block(item, target_root)
        listing_uid = str(uuid.uuid4())
        collection_listing["block"] = listing_uid
        blocks[listing_uid] = collection_listing
        layout.append(listing_uid)

    return blocks, layout


def normalize_language(lang: str | None) -> str:
    """Normalize EPANET language values to the target language."""
    if not lang or lang.lower() in ("", "en-gb", "en_gb"):
        return "en"
    return lang


def normalize_resolveuid_url(url: str, scale: str | None = None) -> str:
    """Normalize a resolveuid reference to a site-root absolute URL.

    Internal references are kept as ``/resolveuid/<uid>`` instead of being
    rewritten to a hardcoded target URL. plone.restapi resolves them at
    serialization time (``uid_to_url``), which keeps the transformed JSON
    host-agnostic and preserves Link items' ``remoteUrl`` behaviour.

    When a scale is known, the ``@@images`` scale suffix is reconstructed so
    inline and standalone images keep their size after conversion.
    """
    if not url or "resolveuid/" not in url:
        return url
    match = re.search(r"resolveuid/([a-f0-9]{32})", url)
    if not match:
        return url
    uid = match.group(1)
    if scale:
        return f"/resolveuid/{uid}/@@images/image/{scale}"
    # Preserve any suffix that follows the UID (e.g. /@@images/image/preview).
    suffix = url[match.end():]
    return f"/resolveuid/{uid}{suffix}"


def normalize_block_urls(value: Any) -> Any:
    """Normalize resolveuid URLs inside Volto blocks, recursively.

    All resolveuid references (links included) are kept in resolveuid form so
    that the target site resolves them against its own content. Only image
    URLs are reconstructed with their known scale.
    """
    if isinstance(value, dict):
        # Standalone image block.
        if value.get("@type") == "image" and isinstance(value.get("url"), str):
            new_value = dict(value)
            new_value["url"] = normalize_resolveuid_url(
                value["url"], value.get("scale")
            )
            return new_value

        # Inline Slate image.
        if value.get("type") == "img" and isinstance(value.get("url"), str):
            new_value = dict(value)
            new_value["url"] = normalize_resolveuid_url(
                value["url"], value.get("scale")
            )
            new_value["children"] = normalize_block_urls(value.get("children", []))
            return new_value

        return {k: normalize_block_urls(v) for k, v in value.items()}

    if isinstance(value, list):
        return [normalize_block_urls(v) for v in value]

    if isinstance(value, str):
        return normalize_resolveuid_url(value)

    return value


def normalize_blocks(blocks: dict[str, Any]) -> None:
    """In-place normalization of resolveuid URLs inside Volto blocks."""
    for block in blocks.values():
        normalized = normalize_block_urls(block)
        block.clear()
        block.update(normalized)


def extract_inline_images(item: dict[str, Any]) -> None:
    """Promote inline Slate images to standalone image blocks.

    The blocks converter turns inline <img> tags into Slate inline images
    (``type: img`` nodes).  For a cleaner Volto layout, extract them into
    standalone ``image`` blocks placed before the Slate block they came from.
    """
    blocks = item.get("blocks")
    layout = item.get("blocks_layout", {}).get("items")
    if not blocks or not layout:
        return

    new_layout: list[str] = []
    new_blocks = dict(blocks)

    for block_id in layout:
        block = new_blocks.get(block_id)
        if block and block.get("@type") == "slate":
            extracted: list[dict[str, Any]] = []
            cleaned_value = _remove_inline_images(block.get("value", []), extracted)

            for img_info in extracted:
                img_block_id = str(uuid.uuid4())
                img_block: dict[str, Any] = {
                    "@type": "image",
                    "url": img_info["url"],
                    "alt": img_info.get("alt", ""),
                    "title": img_info.get("title", ""),
                    "align": img_info.get("align", ""),
                }
                if img_info.get("image_scales"):
                    img_block["image_scales"] = img_info["image_scales"]
                new_blocks[img_block_id] = img_block
                new_layout.append(img_block_id)

            if extracted:
                block["value"] = [
                    node
                    for node in cleaned_value
                    if not _is_empty_slate_node(node)
                ]
                block.pop("plaintext", None)

        new_layout.append(block_id)

    item["blocks"] = new_blocks
    item["blocks_layout"]["items"] = new_layout


def _remove_inline_images(value: Any, extracted: list[dict[str, Any]]) -> Any:
    """Recursively walk a Slate value and extract ``type: img`` nodes.

    Returns the cleaned value tree; removed image nodes are appended to
    ``extracted`` as dicts with url/alt/title/scale/align/image_scales.
    """
    if isinstance(value, list):
        result: list[Any] = []
        for child in value:
            cleaned = _remove_inline_images(child, extracted)
            if cleaned is None:
                continue
            if isinstance(cleaned, list):
                result.extend(cleaned)
            else:
                result.append(cleaned)
        return result

    if isinstance(value, dict):
        if value.get("type") == "img":
            extracted.append(
                {
                    "url": value.get("url"),
                    "alt": value.get("alt", ""),
                    "title": value.get("title", ""),
                    "scale": value.get("scale"),
                    "align": value.get("align", ""),
                    "image_scales": value.get("image_scales"),
                }
            )
            return None

        cleaned = dict(value)
        if "children" in cleaned:
            cleaned["children"] = _remove_inline_images(
                cleaned["children"], extracted
            )
        return cleaned

    return value


def _is_empty_slate_node(node: Any) -> bool:
    """Return True if a Slate node has no visible text."""
    if not isinstance(node, dict):
        return False
    text = node.get("text")
    if text is not None:
        return not str(text).strip()
    children = node.get("children", [])
    if not children:
        return True
    return all(_is_empty_slate_node(child) for child in children)


RESOLVEUID_REF_RE = re.compile(r"resolveuid/([a-f0-9]{32})")


def collect_resolveuid_refs(value: Any) -> set[str]:
    """Return every resolveuid UID referenced inside a block structure."""
    refs: set[str] = set()
    if isinstance(value, dict):
        for child in value.values():
            refs |= collect_resolveuid_refs(child)
    elif isinstance(value, list):
        for child in value:
            refs |= collect_resolveuid_refs(child)
    elif isinstance(value, str):
        refs |= set(RESOLVEUID_REF_RE.findall(value))
    return refs


def find_dangling_links(
    transformed: list[dict[str, Any]],
    raw_data: list[dict[str, Any]],
) -> list[str]:
    """Report resolveuid references whose target was not transformed.

    This catches links to skipped (private) or missing items before import,
    instead of letting them render as broken ``/en/http://...`` URLs.
    """
    valid_uids = {item.get("UID") for item in transformed if item.get("UID")}
    raw_by_uid = {item.get("UID"): item for item in raw_data if item.get("UID")}
    dangling: list[str] = []
    for item in transformed:
        refs = collect_resolveuid_refs(item.get("blocks", {}))
        for uid in sorted(refs - valid_uids):
            target = raw_by_uid.get(uid)
            if target:
                detail = (
                    f"{target.get('@type')} {target.get('@id')} "
                    f"(review_state={target.get('review_state')})"
                )
            else:
                detail = "UID not present in the source export"
            dangling.append(f"{item.get('@id')} -> resolveuid/{uid}: {detail}")
    return dangling


def transform_item(
    item: dict[str, Any],
    old_root: str,
    target_root: str,
    converter_url: str,
    template: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a transformed item dict, or None to skip the item."""
    old_type = item.get("@type", "")
    new_type = TYPE_MAP.get(old_type)
    if new_type is None:
        logger.warning("Skipping unknown type %s at %s", old_type, item.get("@id"))
        return None

    # Build new item from allowed fields only.
    new_item: dict[str, Any] = {}
    for key, value in item.items():
        if key in KEEP_FIELDS:
            new_item[key] = value
        elif key in DROP_FIELDS:
            continue
        else:
            # Unknown field: keep it but warn so we can review.
            logger.debug("Keeping unknown field %s for %s", key, item.get("@id"))
            new_item[key] = value

    new_item["@type"] = new_type
    new_item["language"] = normalize_language(item.get("language"))

    # Rewrite item URL.
    new_item["@id"] = target_url(item["@id"], old_root, target_root)

    # Rewrite parent reference.
    parent = new_item.get("parent", {})
    if parent:
        parent = dict(parent)
        parent["@id"] = target_url(parent["@id"], old_root, target_root)
        if is_top_level(item, old_root):
            parent["@type"] = SUBSITE_TYPE
            parent.pop("UID", None)
        new_item["parent"] = parent

    # Convert text to blocks for page-ish types.
    if new_type in BLOCKS_TYPES:
        blocks, layout = build_blocks(
            item, converter_url, template, target_root
        )
        if blocks:
            new_item["blocks"] = blocks
            new_item["blocks_layout"] = {"items": layout}
        # Drop the legacy text field; it is now represented as blocks.
        new_item.pop("text", None)
    else:
        # File / Image / Link keep their native fields; no blocks.
        new_item.pop("text", None)

    return new_item




def get_default_page_map(data: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map Folder @id to its default-page Document item (if any).

    A default page is inferred when a Folder contains a child Document whose
    id matches the Folder id, or whose title matches the Folder title.
    """
    parent_map: dict[str, list[dict[str, Any]]] = {}
    for item in data:
        parent_id = item.get("parent", {}).get("@id")
        if parent_id:
            parent_map.setdefault(parent_id, []).append(item)

    default_pages: dict[str, dict[str, Any]] = {}
    for item in data:
        if item.get("@type") != "Folder":
            continue
        children = parent_map.get(item["@id"], [])
        docs = [c for c in children if c.get("@type") == "Document"]
        # Prefer same id, then same title.
        for doc in docs:
            if doc["id"] == item["id"]:
                default_pages[item["@id"]] = doc
                break
        else:
            folder_title = item.get("title", "").strip().lower()
            for doc in docs:
                if doc.get("title", "").strip().lower() == folder_title:
                    default_pages[item["@id"]] = doc
                    break
    return default_pages


def merge_default_page_blocks(
    folder_item: dict[str, Any],
    default_page: dict[str, Any],
    converter_url: str,
) -> None:
    """Copy non-title body blocks from a default-page Document onto a Folder."""
    text_field = default_page.get("text")
    html = ""
    if isinstance(text_field, dict):
        html = text_field.get("data", "")
    elif isinstance(text_field, str):
        html = text_field

    if not html or not html.strip():
        return

    body_blocks, body_layout = html_to_blocks(html, converter_url)
    if not body_blocks:
        return

    existing_blocks = folder_item.setdefault("blocks", {})
    existing_layout = folder_item.setdefault("blocks_layout", {"items": []})["items"]

    for uid in body_layout:
        block = body_blocks[uid]
        # Skip the default page's title block; the Folder already has one.
        if block.get("@type") == "title":
            continue
        existing_blocks[uid] = block
        existing_layout.append(uid)

def transform(
    data: list[dict[str, Any]],
    old_root: str,
    target_root: str,
    converter_url: str,
    include_private: bool,
    skip_collections: bool,
    template: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Return (transformed_items, collections, errors)."""
    transformed: list[dict[str, Any]] = []
    collections: list[dict[str, Any]] = []
    errors: list[str] = []
    default_page_map = get_default_page_map(data)

    for idx, item in enumerate(data, start=1):
        item_id = item.get("@id", f"<item {idx}>")
        old_type = item.get("@type")

        if old_type == "Collection":
            if not skip_collections:
                collections.append(item)

        review_state = item.get("review_state")
        if review_state == "private" and not include_private:
            if old_type in PRIVATE_TYPE_EXCEPTIONS:
                logger.info(
                    "Including private %s %s (kept as a link target)",
                    old_type,
                    item_id,
                )
            else:
                logger.info("Skipping private item %s", item_id)
                continue

        try:
            new_item = transform_item(
                item, old_root, target_root, converter_url, template
            )
            if new_item:
                if item.get("@type") == "Folder" and item["@id"] in default_page_map:
                    default_page = default_page_map[item["@id"]]
                    logger.info(
                        "Merging default page %s into Folder %s",
                        default_page["@id"],
                        item["@id"],
                    )
                    merge_default_page_blocks(new_item, default_page, converter_url)
                if new_item.get("blocks"):
                    extract_inline_images(new_item)
                    normalize_blocks(new_item["blocks"])
                transformed.append(new_item)
        except Exception as exc:
            msg = f"Failed to transform {item_id}: {exc}"
            logger.exception(msg)
            errors.append(msg)

    return transformed, collections, errors


def write_report(
    path: Path,
    input_path: Path,
    output_path: Path,
    collections_path: Path,
    old_root: str,
    target_root: str,
    converter_url: str,
    raw_counts: Counter,
    transformed_counts: Counter,
    collections: list[dict[str, Any]],
    errors: list[str],
    dangling_links: list[str],
    include_private: bool,
) -> None:
    lines = [
        "# EPANET Transform Report",
        "",
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}Z",
        "",
        "## Configuration",
        "",
        f"- Input: `{input_path}`",
        f"- Output: `{output_path}`",
        f"- Collections output: `{collections_path}`",
        f"- Old root: `{old_root}`",
        f"- Target root: `{target_root}`",
        f"- Converter URL: `{converter_url}`",
        f"- Include private items: `{include_private}`",
        f"- Private types always included: `{', '.join(sorted(PRIVATE_TYPE_EXCEPTIONS))}`",
        "",
        "## Counts",
        "",
        "| Type | Raw export | Transformed |",
        "|---|---|---|",
    ]
    all_types = sorted(set(raw_counts) | set(transformed_counts))
    for t in all_types:
        lines.append(f"| {t} | {raw_counts.get(t, 0)} | {transformed_counts.get(t, 0)} |")

    lines.extend([
        "",
        f"- **Total raw items:** {sum(raw_counts.values())}",
        f"- **Total transformed items:** {sum(transformed_counts.values())}",
        f"- **Collections converted to Documents:** {len(collections)}",
        f"- **Errors:** {len(errors)}",
        f"- **Dangling internal links:** {len(dangling_links)}",
        "",
    ])

    if collections:
        lines.extend([
            "## Collections (converted to Documents with a listing block)",
            "",
        ])
        for c in collections:
            lines.append(f"- `{c['@id']}`")
            lines.append(f"  - query: `{json.dumps(c.get('query', []))}`")
            lines.append(f"  - sort_on: `{c.get('sort_on')}`")
            lines.append(f"  - sort_reversed: `{c.get('sort_reversed')}`")
            lines.append(f"  - limit: `{c.get('limit')}`")

    if dangling_links:
        lines.extend([
            "",
            f"## Dangling internal links ({len(dangling_links)})",
            "",
            "These resolveuid references point to items that were not transformed",
            "(private or missing). They render broken unless the target is included",
            "or the link is removed.",
            "",
        ])
        for ref in dangling_links:
            lines.append(f"- {ref}")

    if errors:
        lines.extend([
            "",
            "## Errors",
            "",
        ])
        for err in errors:
            lines.append(f"- {err}")

    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    if not args.input.exists():
        logger.error("Input file not found: %s", args.input)
        return 1

    logger.info("Loading export from %s", args.input)
    with args.input.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        logger.error("Expected a JSON array, got %s", type(data).__name__)
        return 1

    template = None
    if args.template.exists():
        with args.template.open("r", encoding="utf-8") as f:
            template = json.load(f)
        logger.info("Using News Item default-blocks template %s", args.template)
    else:
        logger.warning(
            "Template %s not found; News Items will not get default blocks",
            args.template,
        )

    raw_counts = Counter(item.get("@type", "UNKNOWN") for item in data)

    logger.info("Transforming %d items for target %s", len(data), args.target_root)
    transformed, collections, errors = transform(
        data,
        args.old_root,
        args.target_root,
        args.converter_url,
        args.include_private,
        args.skip_collections,
        template,
    )

    transformed_counts = Counter(item["@type"] for item in transformed)

    dangling_links = find_dangling_links(transformed, data)
    if dangling_links:
        logger.warning(
            "Found %d dangling internal link(s); see report", len(dangling_links)
        )

    # Write transformed import JSON.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(transformed, f, indent=2, ensure_ascii=False)
    logger.info("Wrote transformed JSON: %s (%d items)", args.output, len(transformed))

    # Write collections JSON.
    if collections:
        with args.collections_output.open("w", encoding="utf-8") as f:
            json.dump(collections, f, indent=2, ensure_ascii=False)
        logger.info("Wrote collections JSON: %s (%d items)", args.collections_output, len(collections))

    # Write report.
    write_report(
        args.report_output,
        args.input,
        args.output,
        args.collections_output,
        args.old_root,
        args.target_root,
        args.converter_url,
        raw_counts,
        transformed_counts,
        collections,
        errors,
        dangling_links,
        args.include_private,
    )
    logger.info("Wrote report: %s", args.report_output)

    if errors:
        logger.error("Transform completed with %d errors", len(errors))
        return 1

    logger.info("Transform completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
