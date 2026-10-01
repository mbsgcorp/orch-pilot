#!/usr/bin/env python3
"""Report relative Markdown links that point at files which do not exist.

Usage: python3 tools/check_links.py

Scans every *.md file under the repository root (skipping .git/) for inline
links [text](target). External (http://, https://), mailto:, #anchor-only and
absolute-path (/...) targets are skipped. Links inside fenced code blocks and
inline code spans are ignored. Exit code is 0 when nothing is missing, 1 otherwise.
"""

import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parent.parent

SKIP_PREFIXES = ("http://", "https://", "mailto:", "#", "/")
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]*)\)")
INLINE_CODE_RE = re.compile(r"(`+).*?\1")


def find_links(text):
    """Return (line_number, raw_target) for each inline link outside code."""
    links = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), line)
        for match in LINK_RE.finditer(line):
            links.append((number, match.group(1)))
    return links


def clean_target(raw):
    """Strip whitespace and any trailing title: 'file.md "Title"' -> 'file.md'."""
    parts = raw.strip().split()
    return parts[0] if parts else ""


def is_skipped(target):
    return target.startswith(SKIP_PREFIXES)


def target_exists(target, base_dir):
    path = unquote(target.split("#", 1)[0])
    if not path:
        return False
    return (base_dir / path).exists()


def markdown_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            if name.endswith(".md"):
                yield Path(dirpath) / name


def check(root):
    """Return (checked_count, [(relative_file_posix, line, target), ...])."""
    root = Path(root)
    checked = 0
    missing = []
    for md_file in markdown_files(root):
        rel = md_file.relative_to(root).as_posix()
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"warning: cannot read {rel}: {exc.strerror}", file=sys.stderr)
            continue
        for line, raw in find_links(text):
            target = clean_target(raw)
            if is_skipped(target):
                continue
            checked += 1
            if not target_exists(target, md_file.parent):
                missing.append((rel, line, target))
    missing.sort(key=lambda item: (item[0], item[1]))
    return checked, missing


def main(argv=None, root=REPO_ROOT):
    checked, missing = check(root)
    for rel, line, target in missing:
        print(f"{rel}:{line}: missing {target}")
    print(f"checked {checked} links, {len(missing)} missing")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
