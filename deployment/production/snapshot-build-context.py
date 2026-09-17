#!/usr/bin/env python3
"""Capture only image inputs from the working tree; never copy sites or secrets."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2]).resolve()
if target.exists() and any(target.iterdir()):
    raise SystemExit("Snapshot destination must be empty")
target.mkdir(parents=True, exist_ok=True)
package = "custom_apps/process_simplification/process_simplification/"
fixed = {
    "custom_apps/process_simplification/README.md",
    "custom_apps/process_simplification/pyproject.toml",
    "deployment/production/Dockerfile", "deployment/production/Dockerfile.dockerignore",
    "deployment/production/apps.json", "deployment/production/entrypoint.sh",
    "deployment/production/start.sh",
}
names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root).decode().split("\0")
files = {}
for name in sorted(set(names)):
    if not name:
        continue
    is_patch = name.startswith("deployment/production/patches/") and Path(name).suffix in {".py", ".json", ".patch", ".md"}
    if name not in fixed and not name.startswith(package) and not is_patch:
        continue
    source = root / name
    if not source.exists():
        continue  # tracked deletions are absent in the candidate
    if source.is_symlink() or not source.is_file():
        raise SystemExit(f"Unsupported image input: {name}")
    if any(part in {"sites", "secrets", "node_modules", "__pycache__"} for part in Path(name).parts) or source.name.startswith(".env"):
        raise SystemExit(f"Refusing private or generated image input: {name}")
    dest = target / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    files[name] = hashlib.sha256(dest.read_bytes()).hexdigest()
missing = fixed - files.keys()
if missing:
    raise SystemExit(f"Missing image inputs: {sorted(missing)}")
manifest = json.loads((target / "deployment/production/patches/erpnext-patches.json").read_text())
for name, hashes in manifest["files"].items():
    if hashlib.sha256((root / name).read_bytes()).hexdigest() != hashes["patched"]:
        raise SystemExit(f"Working-tree core fix is not represented by the release patch: {name}")
canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
snapshot = hashlib.sha256(canonical).hexdigest()
metadata = {"source_mode": "working-tree", "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode().strip(), "snapshot_sha256": snapshot, "files": files}
(target / "source-manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
print(snapshot)
