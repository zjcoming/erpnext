#!/usr/bin/env python3
"""Apply the audited Frappe patch only to its exact, checksum-pinned sources."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def apply_patches(source, *, check=False):
    directory = Path(__file__).resolve().parent
    manifest = json.loads((directory / "frappe-patches.json").read_text())
    source = Path(source).resolve()
    states = []
    for name, hashes in manifest["files"].items():
        actual = digest(source / name)
        if actual == hashes["patched"]:
            states.append("patched")
        elif actual == hashes["original"]:
            states.append("original")
        else:
            raise SystemExit(f"Refusing changed or unsupported Frappe source: {name}")
    if set(states) == {"patched"}:
        print("Frappe transaction patch already applied; all checksums verified")
        return
    if set(states) != {"original"}:
        raise SystemExit("Refusing partly applied Frappe transaction patch")
    patch = str(directory / manifest["patch"])
    subprocess.run(["git", "apply", "--check", patch], cwd=source, check=True)
    if check:
        print("Frappe transaction patch matches all pinned source checksums")
        return
    subprocess.run(["git", "apply", patch], cwd=source, check=True)
    for name, hashes in manifest["files"].items():
        if digest(source / name) != hashes["patched"]:
            raise SystemExit(f"Patched source failed checksum verification: {name}")
    print("Frappe transaction patch applied and verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Frappe app directory")
    parser.add_argument("--check", action="store_true", help="Verify without modifying source")
    arguments = parser.parse_args()
    apply_patches(arguments.source, check=arguments.check)
