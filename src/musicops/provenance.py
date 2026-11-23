from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


MANIFEST_PATHS = ("pyproject.toml", "README.md", "src")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_manifest(root: str | Path) -> str:
    root_path = Path(root)
    files: list[Path] = []
    for relative in MANIFEST_PATHS:
        candidate = root_path / relative
        if candidate.is_file():
            files.append(candidate)
        elif candidate.is_dir():
            files.extend(path for path in candidate.rglob("*") if path.is_file())
        else:
            raise FileNotFoundError(f"manifest_source_missing:{relative}")
    rows = []
    for path in sorted(files, key=lambda item: item.relative_to(root_path).as_posix()):
        relative = path.relative_to(root_path).as_posix()
        if path.name.startswith("._") or "__pycache__" in path.parts or path.suffix == ".pyc":
            raise ValueError(f"forbidden_source_artifact:{relative}")
        data = path.read_bytes()
        rows.append(f"{_sha256_bytes(data)}  {len(data)}  {relative}")
    return "\n".join(rows) + "\n"


def write_source_manifest(root: str | Path, output: str | Path) -> dict[str, Any]:
    rendered = source_manifest(root)
    output_path = Path(output)
    output_path.write_text(rendered, encoding="utf-8")
    return {
        "manifest_sha256": _sha256_bytes(rendered.encode("utf-8")),
        "file_count": len(rendered.splitlines()),
    }


def verify_source_manifest(
    root: str | Path,
    manifest_path: str | Path,
    *,
    expected_sha256: str = "",
) -> dict[str, Any]:
    path = Path(manifest_path)
    if not path.is_file():
        return {
            "verified": False,
            "error": "source_manifest_missing",
            "expected_sha256": expected_sha256,
        }
    embedded = path.read_bytes()
    embedded_sha256 = _sha256_bytes(embedded)
    actual = source_manifest(root).encode("utf-8")
    actual_sha256 = _sha256_bytes(actual)
    verified = embedded == actual and (not expected_sha256 or embedded_sha256 == expected_sha256.lower())
    return {
        "verified": verified,
        "expected_sha256": expected_sha256.lower(),
        "embedded_sha256": embedded_sha256,
        "actual_sha256": actual_sha256,
        "file_count": len(actual.splitlines()),
        "error": "" if verified else "source_manifest_mismatch",
    }


def _main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    write_parser = subparsers.add_parser("write")
    write_parser.add_argument("--root", required=True)
    write_parser.add_argument("--output", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--root", required=True)
    verify_parser.add_argument("--manifest", required=True)
    verify_parser.add_argument("--expected-sha256", default="")
    args = parser.parse_args()
    if args.command == "write":
        result = write_source_manifest(args.root, args.output)
    else:
        result = verify_source_manifest(args.root, args.manifest, expected_sha256=args.expected_sha256)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("verified", True) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
