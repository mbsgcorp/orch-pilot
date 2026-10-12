#!/usr/bin/env python3
"""Record who last changed each *.js file in one or more local git checkouts.

Usage: python3 tools/script_map_gitinfo.py --repo LABEL=PATH [--repo LABEL=PATH ...]
                                           --out FILE [--branch NAME]

Repo arguments and branch checks follow tools/script_map.py; --branch (default
main) is the branch every repo's HEAD must name. Because real history is
needed, a repo whose PATH/.git is not a directory prints
"error: LABEL branch not verified" and a shallow clone (PATH/.git/shallow
exists) prints "error: LABEL is a shallow clone; full history is needed".
Every repo is validated before git runs for any of them.

For each repo one command is run, without a shell, with a 120 second timeout
and GIT_TERMINAL_PROMPT=0:
    git -C PATH -c core.quotepath=off log --no-renames -z --name-only
        --format=<RS>%H<US>%an<US>%cI
Log order is newest first, so the first commit listing a path is that path's
last change. Only paths ending in ".js" are kept. Merge commits list no files,
so a file's last change is the underlying commit. A missing git prints
"error: git not found"; a failed or timed-out run prints
"error: LABEL git log failed: <last stderr line>" (or "timed out").

The output is JSON, written atomically only after every repo succeeded:
{"version": 1, "generated_at": "YYYY-MM-DDTHH:MM:SSZ", "files":
{"LABEL:rel/path.js": {"commit", "author", "date"}}}; "author" is the commit
author name (never the email) and "date" the commit date in UTC
("YYYY-MM-DDTHH:MM:SSZ"). Keys match tools/script_map.py node ids, which reads
the file with --git-info. One line is printed:
"script_map_gitinfo: N files, R repos -> FILE". The exit code is 0, or 2 on
any usage, input, git or write error (nothing is written then).
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import script_map  # noqa: E402

SEP = "\x1e"
FS = "\x1f"
GIT_TIMEOUT = 120
MAX_STDERR = 200


class GitError(Exception):
    """git log could not be run for a repo; str(error) is the message."""


def git_log(path):
    """Return the stdout of the git log command for the repo at path.

    Raises FileNotFoundError when git cannot be started and GitError with a
    reason on a non-zero exit or a timeout.
    """
    argv = ["git", "-C", str(path), "-c", "core.quotepath=off", "log",
            "--no-renames", "-z", "--name-only", f"--format={SEP}%H{FS}%an{FS}%cI"]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
               GIT_CEILING_DIRECTORIES=str(Path(path).resolve().parent))
    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=GIT_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        raise GitError("timed out") from None
    except OSError:
        raise FileNotFoundError("git") from None
    if result.returncode != 0:
        lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
        raise GitError(lines[-1][:MAX_STDERR] if lines else f"exit {result.returncode}")
    return result.stdout


def utc_date(value):
    """Return an ISO 8601 date with offset converted to "YYYY-MM-DDTHH:MM:SSZ"."""
    return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_log(label, text, files):
    """Add {"LABEL:path": {commit, author, date}} for each .js path in git log output,
    keeping the first (newest) commit seen for a path."""
    current = None
    for token in text.split("\0"):
        if SEP in token:
            header, _, rest = token.split(SEP, 1)[1].partition("\n")
            commit, author, date = (header.split(FS) + ["", ""])[:3]
            current = {"commit": commit, "author": author, "date": utc_date(date)}
            paths = [rest]
        else:
            paths = [token[1:] if token.startswith("\n") else token]
        for path in paths:
            key = f"{label}:{path}"
            if current is not None and path.endswith(".js") and key not in files:
                files[key] = dict(current)
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Record the last commit (author and date) of each .js file in git repos.")
    parser.add_argument("--repo", action="append", required=True,
                        metavar="LABEL=PATH",
                        help="repository to read; repeat for several repos")
    parser.add_argument("--out", required=True, metavar="FILE",
                        help="git info JSON file to write")
    parser.add_argument("--branch", default="main", metavar="NAME",
                        help="branch every repo's HEAD must name (default main)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if not script_map.BRANCH_RE.fullmatch(args.branch):
        print("error: --branch must be a branch name", file=sys.stderr)
        return 2

    repos = []
    labels = set()
    for value in args.repo:
        try:
            label, path = script_map.parse_repo_arg(value)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if label in labels:
            print(f"error: duplicate repo label {label!r}", file=sys.stderr)
            return 2
        if not path.exists():
            print(f"error: {label} path {str(path)!r} does not exist", file=sys.stderr)
            return 2
        if not path.is_dir():
            print(f"error: {label} path {str(path)!r} is not a directory", file=sys.stderr)
            return 2
        labels.add(label)
        repos.append((label, path))

    for label, path in repos:
        try:
            status = script_map.branch_status(path, args.branch)
        except OSError as exc:
            print(f"error: {label} cannot read {path / '.git' / 'HEAD'}: {exc.strerror}",
                  file=sys.stderr)
            return 2
        if status is None:
            print(f"error: {label} branch not verified", file=sys.stderr)
            return 2
        if status != args.branch:
            print(f"error: {label} is on {status}, not {args.branch}", file=sys.stderr)
            return 2
        if (path / ".git" / "shallow").exists():
            print(f"error: {label} is a shallow clone; full history is needed",
                  file=sys.stderr)
            return 2

    files = {}
    for label, path in repos:
        try:
            parse_log(label, git_log(path), files)
        except FileNotFoundError:
            print("error: git not found", file=sys.stderr)
            return 2
        except GitError as exc:
            print(f"error: {label} git log failed: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"error: {label} git log failed: unexpected output: {exc}"[:MAX_STDERR],
                  file=sys.stderr)
            return 2

    data = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": files,
    }
    out = Path(args.out)
    tmp = out.with_name(out.name + ".tmp")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, out)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"error: cannot write {args.out}: {exc}", file=sys.stderr)
        return 2

    print(f"script_map_gitinfo: {len(files)} files, {len(repos)} repos -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
