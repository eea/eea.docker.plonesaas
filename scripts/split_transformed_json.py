#!/usr/bin/env python3
"""Split a collective.exportimport JSON export into smaller importable chunks.

 collective.exportimport's @@import_content form uploads the whole file in one
 HTTP POST. Large exports (many embedded base64 blobs) hit the web server's
 request-body limit and fail with "413 Request Entity Too Large". This script
 splits the export into multiple smaller files that can be imported
 sequentially with the same settings.

 The export order is assumed to be parents-before-children, which
 collective.exportimport guarantees. The script verifies this before splitting.

 Usage:
     python scripts/split_transformed_json.py content-exports/epanet-transformed-demo.json
     python scripts/split_transformed_json.py input.json --output-dir chunks --max-size 5242880
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Path to the transformed JSON file")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for chunk files (default: <input-stem>-chunks)",
    )
    parser.add_argument(
        "--max-size",
        type=int,
        default=10 * 1024 * 1024,
        help="Maximum chunk size in bytes (default: 10 MB)",
    )
    return parser.parse_args(argv)


def verify_order(items: list[dict[str, Any]]) -> list[str]:
    """Return a list of ordering errors (empty if order is valid)."""
    index_by_id = {item["@id"]: idx for idx, item in enumerate(items)}
    errors: list[str] = []
    for idx, item in enumerate(items):
        parent_id = item.get("parent", {}).get("@id")
        if not parent_id:
            continue
        parent_idx = index_by_id.get(parent_id)
        if parent_idx is None:
            # Parent is not in the export (e.g. the existing Subsite).
            continue
        if parent_idx >= idx:
            errors.append(
                f"Item {idx} ({item['@id']}) appears before its parent "
                f"{parent_idx} ({parent_id})"
            )
    return errors


def split_items(
    items: list[dict[str, Any]], max_size: int
) -> list[list[dict[str, Any]]]:
    """Split items into chunks whose JSON serialization is <= max_size bytes."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    empty_list_bytes = json.dumps([]).encode("utf-8")
    current_size: int = len(empty_list_bytes)

    for item in items:
        item_bytes = json.dumps([item], ensure_ascii=False).encode("utf-8")
        item_size = len(item_bytes)

        if current and current_size + item_size > max_size:
            chunks.append(current)
            current = []
            current_size = len(json.dumps([]).encode("utf-8"))

        current.append(item)
        current_size += item_size

        if item_size > max_size:
            print(
                f"WARNING: single item {item['@id']} is {item_size} bytes, "
                f"larger than max_size {max_size}. It will be in a chunk by itself.",
                file=sys.stderr,
            )

    if current:
        chunks.append(current)

    return chunks


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        print("ERROR: input JSON must be a list of items", file=sys.stderr)
        return 1

    print(f"Loaded {len(data)} items from {args.input}")

    errors = verify_order(data)
    if errors:
        print("ERROR: parent-before-child ordering is violated:", file=sys.stderr)
        for err in errors[:10]:
            print(f"  {err}", file=sys.stderr)
        return 1
    print("Order verified: parents appear before children")

    output_dir = args.output_dir or args.input.parent / f"{args.input.stem}-chunks"
    output_dir.mkdir(parents=True, exist_ok=True)

    chunks = split_items(data, args.max_size)

    total_written: int = 0
    for i, chunk in enumerate(chunks, start=1):
        chunk_path = output_dir / f"{args.input.stem}-chunk-{i:03d}.json"
        chunk_data = json.dumps(chunk, ensure_ascii=False, indent=2)
        chunk_bytes = chunk_data.encode("utf-8")
        with open(chunk_path, "wb") as f:
            f.write(chunk_bytes)
        total_written += len(chunk_bytes)
        print(f"  {chunk_path.name}: {len(chunk)} items, {len(chunk_bytes):,} bytes")

    print(f"\nWrote {len(chunks)} chunks to {output_dir}")
    print(f"Total size: {total_written:,} bytes")
    print("\nImport them in order (chunk-001 first) using the same settings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
