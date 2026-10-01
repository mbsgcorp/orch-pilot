import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import check_links  # noqa: E402

SCRIPT = TOOLS_DIR / "check_links.py"


class CheckLinksTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text=""):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_main(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check_links.main([], root=self.root)
        return code, out.getvalue().splitlines()

    def test_good_link_passes(self):
        self.write("other.md")
        self.write("README.md", "See [other](other.md).\n")
        self.assertEqual(check_links.check(self.root), (1, []))

    def test_missing_file_reported_with_line_number(self):
        self.write("README.md", "line one\nline two\nSee [gone](gone.md).\n")
        code, lines = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(lines, ["README.md:3: missing gone.md",
                                 "checked 1 links, 1 missing"])

    def test_external_mailto_and_anchor_links_skipped(self):
        self.write("README.md",
                   "[a](http://example.com/x.md) [b](https://example.com/y.md)\n"
                   "[c](mailto:someone@example.com) [d](#section)\n")
        self.assertEqual(check_links.check(self.root), (0, []))

    def test_anchor_suffix_checks_file(self):
        self.write("file.md")
        self.write("README.md", "[ok](file.md#section) [bad](nope.md#section)\n")
        checked, missing = check_links.check(self.root)
        self.assertEqual(checked, 2)
        self.assertEqual(missing, [("README.md", 1, "nope.md#section")])

    def test_fenced_code_ignored(self):
        self.write("README.md",
                   "```\n[x](missing1.md)\n```\n"
                   "  ```python\n[y](missing2.md)\n  ```\n"
                   "[z](missing3.md)\n")
        checked, missing = check_links.check(self.root)
        self.assertEqual(checked, 1)
        self.assertEqual(missing, [("README.md", 7, "missing3.md")])

    def test_inline_code_ignored(self):
        self.write("README.md",
                   "Use `[x](missing.md)` or ``[y](also.md)`` here.\n")
        self.assertEqual(check_links.check(self.root), (0, []))

    def test_directory_link_passes(self):
        (self.root / "docs").mkdir()
        self.write("README.md", "[docs](docs) and [slash](docs/)\n")
        self.assertEqual(check_links.check(self.root), (2, []))

    def test_exit_code_zero_when_nothing_missing(self):
        self.write("other.md")
        self.write("README.md", "[other](other.md)\n")
        code, lines = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["checked 1 links, 0 missing"])

    def test_exit_code_one_when_missing(self):
        self.write("README.md", "[gone](gone.md)\n")
        code, _ = self.run_main()
        self.assertEqual(code, 1)

    def test_summary_line_counts(self):
        self.write("a.md")
        self.write("README.md",
                   "[a](a.md) [b](b.md) [c](c.md) [web](https://example.com)\n")
        code, lines = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], "checked 3 links, 2 missing")

    def test_no_markdown_files(self):
        code, lines = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["checked 0 links, 0 missing"])

    def test_percent20_decoded(self):
        self.write("my file.md")
        self.write("README.md", "[f](my%20file.md)\n")
        self.assertEqual(check_links.check(self.root), (1, []))

    def test_two_links_on_one_line(self):
        self.write("README.md", "[a](a.md) and [b](b.md)\n")
        checked, missing = check_links.check(self.root)
        self.assertEqual(checked, 2)
        self.assertEqual(missing, [("README.md", 1, "a.md"),
                                   ("README.md", 1, "b.md")])

    def test_trailing_title_ignored(self):
        self.write("file.md")
        self.write("README.md", '[t](file.md "Title")\n')
        self.assertEqual(check_links.check(self.root), (1, []))

    def test_empty_target_reported(self):
        self.write("README.md", "[t]()\n")
        code, lines = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(lines, ["README.md:1: missing ",
                                 "checked 1 links, 1 missing"])

    def test_subdirectory_relative_link(self):
        self.write("README.md")
        self.write("docs/guide.md", "[up](../README.md) [bad](../NOPE.md)\n")
        checked, missing = check_links.check(self.root)
        self.assertEqual(checked, 2)
        self.assertEqual(missing, [("docs/guide.md", 1, "../NOPE.md")])

    def test_git_dir_skipped(self):
        self.write(".git/notes.md", "[gone](gone.md)\n")
        self.assertEqual(check_links.check(self.root), (0, []))

    def test_output_sorted_by_file_then_line(self):
        self.write("b.md", "[x](x.md)\n\n[y](y.md)\n")
        self.write("a.md", "\n[z](z.md)\n")
        self.write("sub/c.md", "[w](w.md)\n")
        code, lines = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(lines, ["a.md:2: missing z.md",
                                 "b.md:1: missing x.md",
                                 "b.md:3: missing y.md",
                                 "sub/c.md:1: missing w.md",
                                 "checked 4 links, 4 missing"])

    def test_unreadable_file_warns_and_continues(self):
        self.write("a.md", "[gone](gone.md)\n")
        self.write("b.md", "[gone](gone.md)\n")
        real_read_text = Path.read_text

        def fake_read_text(path, *args, **kwargs):
            if path.name == "a.md":
                raise PermissionError(13, "Permission denied")
            return real_read_text(path, *args, **kwargs)

        err = io.StringIO()
        with mock.patch.object(Path, "read_text", fake_read_text), \
                contextlib.redirect_stderr(err):
            checked, missing = check_links.check(self.root)
        self.assertEqual((checked, missing), (1, [("b.md", 1, "gone.md")]))
        self.assertIn("warning: cannot read a.md", err.getvalue())

    def test_find_links_line_numbers(self):
        text = "[a](a.md)\n```\n[b](b.md)\n```\nx [c](c.md)\n"
        self.assertEqual(check_links.find_links(text), [(1, "a.md"), (5, "c.md")])

    def test_runs_from_any_directory(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.root,
                                capture_output=True, text=True)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        last = result.stdout.strip().splitlines()[-1]
        self.assertRegex(last, r"^checked \d+ links, \d+ missing$")


if __name__ == "__main__":
    unittest.main()
