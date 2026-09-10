"""Verify patch reproducibility and fail-closed checksum checks on source copies."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


DIRECTORY = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("PS_FRAPPE_SOURCE", "development/frappe-bench/apps/frappe")).resolve()
MANIFEST = json.loads((DIRECTORY / "frappe-patches.json").read_text())


class TestPatchApplication(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="frappe-patch-check-")
        self.target = Path(self.temporary.name)
        for name in MANIFEST["files"]:
            destination = self.target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / name, destination)

    def tearDown(self):
        self.temporary.cleanup()

    def run_apply(self, *arguments):
        return subprocess.run(
            [sys.executable, str(DIRECTORY / "apply_frappe_patches.py"), str(self.target), *arguments],
            capture_output=True, text=True,
        )

    def test_check_apply_and_reapply_are_reproducible(self):
        original = {name: (self.target / name).read_bytes() for name in MANIFEST["files"]}
        self.assertEqual(self.run_apply("--check").returncode, 0)
        self.assertEqual(original, {name: (self.target / name).read_bytes() for name in MANIFEST["files"]})
        applied = self.run_apply()
        self.assertEqual(applied.returncode, 0, applied.stderr)
        first = {name: (self.target / name).read_bytes() for name in MANIFEST["files"]}
        repeated = self.run_apply()
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(first, {name: (self.target / name).read_bytes() for name in MANIFEST["files"]})

    def test_unsupported_source_is_rejected_without_partial_changes(self):
        name = next(iter(MANIFEST["files"]))
        with (self.target / name).open("a") as stream:
            stream.write("\n# An unreviewed upstream or local change\n")
        original = {name: (self.target / name).read_bytes() for name in MANIFEST["files"]}
        rejected = self.run_apply()
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("Refusing changed or unsupported", rejected.stderr)
        self.assertEqual(original, {name: (self.target / name).read_bytes() for name in MANIFEST["files"]})

    def test_partly_applied_source_is_rejected_without_more_changes(self):
        name = next(iter(MANIFEST["files"]))
        original_file = (self.target / name).read_bytes()
        self.assertEqual(self.run_apply().returncode, 0)
        (self.target / name).write_bytes(original_file)
        partly_applied = {name: (self.target / name).read_bytes() for name in MANIFEST["files"]}
        rejected = self.run_apply()
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("partly applied", rejected.stderr)
        self.assertEqual(partly_applied, {name: (self.target / name).read_bytes() for name in MANIFEST["files"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
