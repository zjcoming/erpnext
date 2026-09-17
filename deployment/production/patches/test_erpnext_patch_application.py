"""Check the actual pinned upstream input, reproducibility, and drift rejection."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

DIRECTORY = Path(__file__).resolve().parent
ROOT = DIRECTORY.parents[2]
MANIFEST = json.loads((DIRECTORY / "erpnext-patches.json").read_text())

class TestERPNextPatch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="erpnext-release-patch-")
        self.target = Path(self.temp.name)
        for name in MANIFEST["files"]:
            path = self.target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(subprocess.check_output(["git", "show", MANIFEST["baseline_commit"] + ":" + name], cwd=ROOT))
    def tearDown(self):
        self.temp.cleanup()
    def run_apply(self, *args):
        return subprocess.run([sys.executable, str(DIRECTORY / "apply_erpnext_patches.py"), str(self.target), *args], capture_output=True, text=True)
    def contents(self):
        return {name: (self.target / name).read_bytes() for name in MANIFEST["files"]}
    def test_readonly_check_apply_reapply_match_working_tree(self):
        original = self.contents()
        result = self.run_apply("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(original, self.contents())
        for _ in range(2):
            result = self.run_apply()
            self.assertEqual(result.returncode, 0, result.stderr)
            for name, hashes in MANIFEST["files"].items():
                self.assertEqual(hashlib.sha256((self.target / name).read_bytes()).hexdigest(), hashes["patched"])
                self.assertEqual((self.target / name).read_bytes(), (ROOT / name).read_bytes())
    def test_unknown_source_rejected_without_writes(self):
        path = self.target / next(iter(MANIFEST["files"]))
        path.write_bytes(path.read_bytes() + b"\n# upstream drift\n")
        before = self.contents()
        result = self.run_apply()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing changed or unsupported", result.stderr)
        self.assertEqual(before, self.contents())

if __name__ == "__main__":
    unittest.main(verbosity=2)
