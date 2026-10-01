import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import doc_stats  # noqa: E402

SCRIPT = TOOLS_DIR / "doc_stats.py"


class DocStatsTest(unittest.TestCase):
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

    def run_main(self, argv=()):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doc_stats.main(list(argv), root=self.root)
        return code, out.getvalue().splitlines()

    def test_word_and_heading_counts(self):
        self.write("README.md", "# Title\n\nsome body words here\n\n## Sub\nmore text\n")
        self.assertEqual(doc_stats.collect(self.root), [("README.md", 10, 2)])

    def test_hashtag_and_seven_hashes_not_headings(self):
        self.assertEqual(doc_stats.count_text("#hashtag\n####### seven\n###### six\n"),
                         (5, 1))

    def test_fenced_code_excluded(self):
        text = ("intro words\n"
                "```\ncode inside fence\n# not heading\n```\n"
                "  ```python\nmore code here\n  ```\n"
                "# After\n")
        self.assertEqual(doc_stats.count_text(text), (4, 1))

    def test_unclosed_fence_excludes_rest(self):
        text = "# Top\none two\n```\nthree four\n# hidden\nfive\n"
        self.assertEqual(doc_stats.count_text(text), (4, 1))

    def test_sort_by_words_desc_then_path(self):
        self.write("b.md", "one two\n")
        self.write("a.md", "one two\n")
        self.write("sub/c.md", "one two three\n")
        self.write("sub/a.md", "one two\n")
        self.write("z.md", "one\n")
        self.assertEqual([row[0] for row in doc_stats.collect(self.root)],
                         ["sub/c.md", "a.md", "b.md", "sub/a.md", "z.md"])

    def test_min_words_filters_rows_but_not_total(self):
        self.write("big.md", "one two three four five\n")
        self.write("small.md", "one two\n")
        code, lines = self.run_main(["--min-words", "3"])
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("file"))
        self.assertTrue(lines[1].startswith("big.md"))
        self.assertNotIn("small.md", "\n".join(lines))
        self.assertEqual(lines[-1], "total: 2 files, 7 words")

    def test_min_words_hiding_everything_prints_only_total(self):
        self.write("a.md", "one\n")
        code, lines = self.run_main(["--min-words", "5"])
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["total: 1 files, 1 words"])

    def test_total_line_format(self):
        self.write("a.md", "one two\n")
        self.write("sub/b.md", "three four five\n")
        code, lines = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], "total: 2 files, 5 words")

    def test_empty_file_shows_zero(self):
        self.write("empty.md")
        code, lines = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(doc_stats.collect(self.root), [("empty.md", 0, 0)])
        self.assertEqual(lines[1].split(), ["empty.md", "0", "0"])
        self.assertEqual(lines[-1], "total: 1 files, 0 words")

    def test_no_markdown_files(self):
        self.write("notes.txt", "not markdown\n")
        code, lines = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["total: 0 files, 0 words"])

    def test_git_dir_skipped(self):
        self.write(".git/notes.md", "lots of words in git\n")
        self.write("a.md", "one\n")
        self.assertEqual(doc_stats.collect(self.root), [("a.md", 1, 0)])

    def test_non_utf8_bytes_do_not_crash(self):
        (self.root / "bad.md").write_bytes(b"caf\xe9 ok\n")
        code, lines = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(doc_stats.collect(self.root), [("bad.md", 2, 0)])
        self.assertEqual(lines[-1], "total: 1 files, 2 words")

    def test_header_and_columns_line_up(self):
        self.write("a-long-file-name.md", "# H\n" + "word " * 120 + "\n")
        self.write("sub/b.md", "# One\n## Two\nx\n")
        self.write("c.md", "")
        code, lines = self.run_main()
        self.assertEqual(code, 0)
        header, rows = lines[0], lines[1:-1]
        self.assertEqual(header.split(), ["file", "words", "headings"])
        self.assertEqual(len(rows), 3)
        widths = {len(line) for line in lines[:-1]}
        self.assertEqual(len(widths), 1)
        words_end = header.index("words") + len("words")
        for row in rows:
            path, words, headings = row.split()
            self.assertTrue(row.startswith(path))
            self.assertEqual(row[words_end - len(words):words_end], words)
            self.assertEqual(row[words_end - len(words) - 1], " ")
            self.assertTrue(row.endswith(headings))
        self.assertEqual([row.split()[0] for row in rows],
                         ["a-long-file-name.md", "sub/b.md", "c.md"])

    def test_runs_from_any_directory(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        last = result.stdout.strip().splitlines()[-1]
        self.assertRegex(last, r"^total: \d+ files, \d+ words$")


if __name__ == "__main__":
    unittest.main()
