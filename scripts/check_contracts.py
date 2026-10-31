#!/usr/bin/env python3
"""Check contract inventory and independent test entrypoints; this is not runtime proof."""

import argparse
import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
STAGES = ("baseline", "python", "go")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=STAGES, default="baseline")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "contracts/runtime.json").read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError("Unknown contract inventory schema")
    tests, ids, missing = set(), set(), []
    for file in (ROOT / "tests/contracts").glob("test_*.py"):
        tree = ast.parse(file.read_text())
        for node in ast.walk(tree):
            names = ([node.module] if isinstance(node, ast.ImportFrom) and node.module else
                     [name.name for name in node.names] if isinstance(node, ast.Import) else [])
            if any(name.startswith("runtime") or name in ("common", "gateway", "configure") for name in names):
                raise ValueError(f"Contract imports implementation internals: {file.name}")
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            for function in cls.body:
                if isinstance(function, ast.FunctionDef) and function.name.startswith("test_"):
                    tests.add(f"contracts.{file.stem}.{cls.name}.{function.name}")
    required = []
    for item in manifest["requirements"]:
        if item["id"] in ids or item["stage"] not in STAGES:
            raise ValueError("Duplicate contract ID or invalid stage")
        ids.add(item["id"])
        if "test" in item and "tests" in item:
            raise ValueError(f"Use test or tests, not both, for {item['id']}")
        if "tests" in item:
            entries = item["tests"]
            if not isinstance(entries, list):
                raise ValueError(f"tests must be a list for {item['id']}")
        else:
            entries = [] if item.get("test") is None else [item["test"]]
        if any(not isinstance(entry, str) or entry not in tests for entry in entries):
            raise ValueError(f"Missing test entrypoint for {item['id']}")
        if STAGES.index(item["stage"]) <= STAGES.index(args.stage):
            required.append(item["id"])
            if not entries:
                missing.append(item["id"])
    print(json.dumps({"stage": args.stage, "required": required, "missing": missing,
                      "note": "Static inventory check only; run the actual required test layer."}, indent=2))
    return int(bool(missing))


if __name__ == "__main__":
    sys.exit(main())
