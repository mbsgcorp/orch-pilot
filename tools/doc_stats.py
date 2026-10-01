#!/usr/bin/env python3
"""Show word and heading counts for every Markdown file in the repository.

Usage: python3 tools/doc_stats.py [--min-words N]

Scans every *.md file under the repository root (skipping .git/) and prints a
table with columns file, words and headings, sorted by words descending, then
by file path. Words are whitespace-separated tokens outside fenced code blocks
(fence lines included in the exclusion; an unclosed fence excludes everything
after it). Headings are lines outside fences starting with one to six '#'
followed by a space. --min-words N shows only files with at least N words; the
final "total: <files> files, <words> words" line always counts every file.
Exit code is 0.
"""

import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

HEADING_RE = re.compile(r"^#{1,6} ")


def count_text(text):
    """Return (words, headings) for text, ignoring fenced code blocks."""
    words = 0
    headings = 0
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        words += len(line.split())
        if HEADING_RE.match(line):
            headings += 1
    return words, headings


def markdown_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            if name.endswith(".md"):
                yield Path(dirpath) / name


def collect(root):
    """Return [(relative_file_posix, words, headings), ...] sorted by size."""
    root = Path(root)
    rows = []
    for md_file in markdown_files(root):
        rel = md_file.relative_to(root).as_posix()
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"warning: cannot read {rel}: {exc.strerror}", file=sys.stderr)
            continue
        words, headings = count_text(text)
        rows.append((rel, words, headings))
    rows.sort(key=lambda row: (-row[1], row[0]))
    return rows


def format_table(rows):
    header = ("file", "words", "headings")
    cells = [header] + [(path, str(words), str(headings))
                        for path, words, headings in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(3)]
    return [f"{path:<{widths[0]}}  {words:>{widths[1]}}  {headings:>{widths[2]}}"
            for path, words, headings in cells]


def main(argv=None, root=REPO_ROOT):
    parser = argparse.ArgumentParser(
        description="Show word and heading counts for Markdown files.")
    parser.add_argument("--min-words", type=int, default=0,
                        help="only show files with at least N words")
    args = parser.parse_args(argv)
    rows = collect(root)
    shown = [row for row in rows if row[1] >= args.min_words]
    if shown:
        for line in format_table(shown):
            print(line)
    total_words = sum(words for _, words, _ in rows)
    print(f"total: {len(rows)} files, {total_words} words")
    return 0


if __name__ == "__main__":
    sys.exit(main())
