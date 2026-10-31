#!/usr/bin/env python3
"""Fail closed on govulncheck JSON findings, even when the scanner exits zero."""
import argparse
import hashlib
import json
from pathlib import Path
import sys


def evaluate(path):
    raw = path.read_bytes()
    remaining = raw.decode("utf-8")
    decoder = json.JSONDecoder()
    config, sbom, findings = None, None, set()
    while remaining.strip():
        message, offset = decoder.raw_decode(remaining.lstrip())
        remaining = remaining.lstrip()[offset:]
        if not isinstance(message, dict) or "error" in message:
            raise ValueError("Malformed or failed scan")
        if "config" in message:
            if config is not None:
                raise ValueError("Duplicate scanner config")
            config = message["config"]
        if "SBOM" in message:
            if sbom is not None:
                raise ValueError("Duplicate scanner SBOM")
            sbom = message["SBOM"]
        if "finding" in message:
            value = message["finding"]
            if not isinstance(value, dict) or not value.get("osv"):
                raise ValueError("Malformed vulnerability finding")
            findings.add(value["osv"])
    if (not isinstance(config, dict) or config.get("scanner_name") != "govulncheck"
            or config.get("scan_level") != "symbol" or config.get("scan_mode") != "source"
            or not isinstance(sbom, dict) or not isinstance(sbom.get("modules"), list)
            or not sbom["modules"] or not all(isinstance(m, dict) for m in sbom["modules"])):
        raise ValueError("A complete source/symbol govulncheck report with SBOM is required")
    if not any(m.get("path") == "github.com/zopiya/cgit" for m in sbom["modules"]):
        raise ValueError("Scan does not cover this project")
    return {"schema_version": 1, "ok": not findings, "findings": sorted(findings),
            "input_sha256": hashlib.sha256(raw).hexdigest(), "scanner": config,
            "note": "Go source dependency scan; not an OS/container vulnerability scan"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = evaluate(args.input)
    except (OSError, ValueError, UnicodeError, TypeError) as exc:
        report = {"schema_version": 1, "ok": False, "error": str(exc)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
