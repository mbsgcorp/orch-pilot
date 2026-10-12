import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ScriptMapRebuildFunctionTest(unittest.TestCase):
    def test_node_suite_passes(self):
        node = shutil.which("node")
        if node is None:
            self.fail("node is required for the rebuild function tests but was not found")
        result = subprocess.run([node, "--test", "tests/script_map_rebuild.test.mjs"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
