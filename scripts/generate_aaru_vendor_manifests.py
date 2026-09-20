#!/usr/bin/env python3
"""Generate chunked Git tree payloads for vendoring this repo into AARU.

The generated JSON files are directly consumable as ``tree_elements`` by the
GitHub create-tree API. Runtime data, caches, Git metadata and the generated
manifests themselves are excluded.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Iterable, List

EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "vendor_manifests",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".wal", ".shm"}
EXCLUDED_PREFIXES = ("data/raw/", "data/databases/")


def included(path: Path, root: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    if any(part in EXCLUDED_PARTS for part in path.relative_to(root).parts):
        return False
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    return not relative.startswith(EXCLUDED_PREFIXES)


def mode_for(path: Path) -> str:
    return "100755" if path.stat().st_mode & 0o111 else "100644"


def entries(root: Path, prefix: str) -> Iterable[Dict[str, str]]:
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if not included(path, root):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise SystemExit(f"Non-UTF-8 file requires explicit handling: {path}")
        yield {
            "path": f"{prefix}/{path.relative_to(root).as_posix()}",
            "mode": mode_for(path),
            "type": "blob",
            "content": content,
        }


def write_chunks(values: List[Dict[str, str]], output: Path, max_bytes: int) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for old in output.glob("manifest_*.json"):
        old.unlink()
    chunks: List[List[Dict[str, str]]] = []
    current: List[Dict[str, str]] = []
    size = 2
    for value in values:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if current and size + len(encoded) + 2 > max_bytes:
            chunks.append(current)
            current = []
            size = 2
        current.append(value)
        size += len(encoded) + 1
    if current:
        chunks.append(current)
    for index, chunk in enumerate(chunks, 1):
        (output / f"manifest_{index:03d}.json").write_text(
            json.dumps(chunk, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    (output / "index.json").write_text(
        json.dumps(
            {
                "schema": "aaru.vendor-manifest-index.v1",
                "source_commit": "GENERATED_AT_COMMIT",
                "prefix": values[0]["path"].split("/upstream/")[0] + "/upstream" if values else "",
                "files": len(values),
                "chunks": len(chunks),
                "chunk_files": [f"manifest_{i:03d}.json" for i in range(1, len(chunks) + 1)],
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("vendor_manifests"))
    parser.add_argument("--prefix", default="tools/aaru_market_data/upstream")
    parser.add_argument("--max-bytes", type=int, default=85000)
    args = parser.parse_args()
    values = list(entries(args.root.resolve(), args.prefix.rstrip("/")))
    write_chunks(values, args.output, args.max_bytes)
    print(json.dumps({"files": len(values), "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
