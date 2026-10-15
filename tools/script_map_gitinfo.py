#!/usr/bin/env python3
"""Record who last changed each *.js file in one or more local git checkouts.

Usage: python3 tools/script_map_gitinfo.py --repo LABEL=PATH [--repo LABEL=PATH ...]
                                           --out FILE [--branch NAME]
                                           [--changes-days N]

Repo arguments and branch checks follow tools/script_map.py; --branch (default
main) is the branch every repo's HEAD must name. Because real history is
needed, a repo whose PATH/.git is not a directory prints
"error: LABEL branch not verified" and a shallow clone (PATH/.git/shallow
exists) prints "error: LABEL is a shallow clone; full history is needed".
Every repo is validated before git runs for any of them.

For each repo one command is run, without a shell, with a 120 second timeout
and GIT_TERMINAL_PROMPT=0:
    git -C PATH -c core.quotepath=off log --no-renames -z --name-only
        --format=<RS>%H<US>%an<US>%cI<US>%s
Log order is newest first, so the first commit listing a path is that path's
last change. Only paths ending in ".js" are kept. Merge commits list no files,
so a file's last change is the underlying commit. A missing git prints
"error: git not found"; a failed or timed-out run prints
"error: LABEL git log failed: <last stderr line>" (or "timed out").

After a repo's log succeeds, a second log is run the same way (same
environment, timeout and error messages) to list recent changes:
    git -C PATH -c core.quotepath=off log --no-renames -z --name-status
        --since=<CUTOFF> --format=<RS>%H<US>%an<US>%cI<US>%s
CUTOFF is now (UTC) minus --changes-days N days (an integer from 1 to 365,
default 30; anything else prints "error: --changes-days must be an integer
from 1 to 365" and exits 2), formatted "YYYY-MM-DDTHH:MM:SSZ" and computed
once per run.

After a repo's log succeeds, `git -C PATH config --get remote.origin.url` is
run the same way. Only the "OWNER/REPO" slug of a GitHub origin (https, with
any userinfo discarded, git@github.com: or ssh://git@github.com/) is kept;
the URL itself is never written or printed. A failed lookup, a missing origin
or a non-GitHub URL gives "" and prints
"warning: LABEL has no GitHub remote; links omitted" to stderr (not an error).

The output is JSON, written atomically only after every repo succeeded:
{"version": 1, "generated_at": "YYYY-MM-DDTHH:MM:SSZ", "repos":
{"LABEL": "OWNER/REPO" or ""}, "files": {"LABEL:rel/path.js": {"commit",
"author", "date", "subject", "pr", "history"}}}; "author" is the commit
author name (never the email) and "date" the commit date in UTC
("YYYY-MM-DDTHH:MM:SSZ"). "subject" is the stripped commit subject cut to 200
characters, "pr" the digits N when the subject ends with "(#N)" (else ""), and
"history" holds up to 6 {commit, author, date, subject, pr} objects for the
commits listing the path (the current commit plus up to 5 previous), newest
first (history[0] is the last change). Keys match tools/script_map.py node
ids, which reads the file with --git-info.

The output also holds "changes_since": CUTOFF and "changes": a list of
{"repo": LABEL, "commit", "author", "date", "subject", "pr", "files":
[{"path", "status"}]}, one per commit in the window that lists at least one
".js" path. "files" keeps only the ".js" paths, in log order; "status" is
"added" for git status A, "removed" for D and "modified" for anything else.
The commit fields follow the rules of the "files" entries above. Commits of
all repos are merged, sorted by "date" newest first (ties by repo label, then
log order) and capped at the newest 500. Merge commits list no files, so
they never appear. One
line is printed: "script_map_gitinfo: N files, R repos -> FILE". The exit
code is 0, or 2 on any usage, input, git or write error (nothing is written
then).
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import script_map  # noqa: E402

SEP = "\x1e"
FS = "\x1f"
GIT_TIMEOUT = 120
MAX_STDERR = 200
MAX_SUBJECT = 200
MAX_HISTORY = 6
MAX_CHANGES = 500
MAX_CHANGES_DAYS = 365
CHANGE_STATUSES = {"A": "added", "D": "removed"}
PR_RE = re.compile(r"\(#([0-9]{1,9})\)$")
_SLUG = r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?"
SLUG_PATTERNS = (
    re.compile(r"https://(?:[^@/]*@)?(?i:github\.com)/" + _SLUG),
    re.compile(r"git@(?i:github\.com):" + _SLUG),
    re.compile(r"ssh://git@(?i:github\.com)/" + _SLUG),
)


class GitError(Exception):
    """git log could not be run for a repo; str(error) is the message."""


def git_env(path):
    """Return the environment every git command runs with for the repo at path."""
    return dict(os.environ, GIT_TERMINAL_PROMPT="0",
                GIT_CEILING_DIRECTORIES=str(Path(path).resolve().parent))


def git_log(path):
    """Return the stdout of the git log command for the repo at path.

    Raises FileNotFoundError when git cannot be started and GitError with a
    reason on a non-zero exit or a timeout.
    """
    return run_log(path, ["git", "-C", str(path), "-c", "core.quotepath=off", "log",
                          "--no-renames", "-z", "--name-only",
                          f"--format={SEP}%H{FS}%an{FS}%cI{FS}%s"])


def git_changes(path, since):
    """Return the stdout of the --name-status git log since the UTC date since.

    Raises like git_log().
    """
    return run_log(path, ["git", "-C", str(path), "-c", "core.quotepath=off", "log",
                          "--no-renames", "-z", "--name-status", f"--since={since}",
                          f"--format={SEP}%H{FS}%an{FS}%cI{FS}%s"])


def run_log(path, argv):
    """Run a git log argv for the repo at path and return its stdout (see git_log)."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=GIT_TIMEOUT, env=git_env(path))
    except subprocess.TimeoutExpired:
        raise GitError("timed out") from None
    except OSError:
        raise FileNotFoundError("git") from None
    if result.returncode != 0:
        lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
        raise GitError(lines[-1][:MAX_STDERR] if lines else f"exit {result.returncode}")
    return result.stdout


def github_slug(url):
    """Return "OWNER/REPO" for a GitHub remote URL, else ""."""
    if not isinstance(url, str):
        return ""
    for pattern in SLUG_PATTERNS:
        match = pattern.fullmatch(url)
        if match:
            owner, repo = match.group("owner"), match.group("repo")
            if owner in (".", "..") or repo in (".", ".."):
                return ""
            return f"{owner}/{repo}"
    return ""


def git_remote_slug(path):
    """Return the GitHub slug of the repo's origin remote, or "" on any failure.

    The remote URL is only passed to github_slug(); it is never returned or printed.
    """
    argv = ["git", "-C", str(path), "config", "--get", "remote.origin.url"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=GIT_TIMEOUT, env=git_env(path))
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    return github_slug((result.stdout or "").strip())


def utc_date(value):
    """Return an ISO 8601 date with offset converted to "YYYY-MM-DDTHH:MM:SSZ"."""
    return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def commit_header(header):
    """Return {commit, author, date, subject, pr} from a "%H<US>%an<US>%cI<US>%s" header."""
    commit, author, date, subject = (header.split(FS, 3) + ["", "", ""])[:4]
    stripped = subject.strip()
    match = PR_RE.search(stripped)
    return {"commit": commit, "author": author, "date": utc_date(date),
            "subject": stripped[:MAX_SUBJECT], "pr": match.group(1) if match else ""}


def parse_log(label, text, files):
    """Add {"LABEL:path": {commit, author, date, subject, pr, history}} for each .js
    path in git log output. The first (newest) commit seen for a path sets the
    top-level fields; up to MAX_HISTORY commits are kept in "history"."""
    current = None
    for token in text.split("\0"):
        if SEP in token:
            header, _, rest = token.split(SEP, 1)[1].partition("\n")
            current = commit_header(header)
            paths = [rest]
        else:
            paths = [token[1:] if token.startswith("\n") else token]
        for path in paths:
            key = f"{label}:{path}"
            if current is None or not path.endswith(".js"):
                continue
            if key not in files:
                files[key] = dict(current, history=[])
            history = files[key]["history"]
            if len(history) < MAX_HISTORY:
                history.append(dict(current))
    return files


def parse_changes(label, text):
    """Return [{repo, commit, author, date, subject, pr, files}] in log order for the
    commits in --name-status -z git log output that list at least one .js path.

    After each header the tokens alternate status and path; a status may share the
    header's token after a newline or start with a newline itself.
    """
    changes = []
    current = None
    status = None
    for token in text.split("\0"):
        if SEP in token:
            header, _, rest = token.split(SEP, 1)[1].partition("\n")
            current = dict(commit_header(header), repo=label, files=[])
            changes.append(current)
            status = rest or None
            continue
        if token.startswith("\n"):
            token = token[1:]
        if current is None:
            continue
        if status is None:
            status = token or None
            continue
        if token.endswith(".js"):
            current["files"].append({"path": token,
                                     "status": CHANGE_STATUSES.get(status[:1], "modified")})
        status = None
    return [change for change in changes if change["files"]]


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
    parser.add_argument("--changes-days", default="30", metavar="N",
                        help="days of recent .js changes to list, 1-365 (default 30)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if not script_map.BRANCH_RE.fullmatch(args.branch):
        print("error: --branch must be a branch name", file=sys.stderr)
        return 2
    days = args.changes_days
    if not (days.isascii() and days.isdigit() and 1 <= int(days) <= MAX_CHANGES_DAYS):
        print("error: --changes-days must be an integer from 1 to 365", file=sys.stderr)
        return 2
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

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
    slugs = {}
    recent = []
    for label, path in repos:
        try:
            parse_log(label, git_log(path), files)
            for index, change in enumerate(parse_changes(label, git_changes(path, cutoff))):
                recent.append((change["date"], label, index, change))
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
        slugs[label] = git_remote_slug(path)

    recent.sort(key=lambda item: (item[1], item[2]))
    recent.sort(key=lambda item: item[0], reverse=True)
    data = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repos": slugs,
        "files": files,
        "changes_since": cutoff,
        "changes": [item[3] for item in recent[:MAX_CHANGES]],
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

    for label, _ in repos:
        if not slugs[label]:
            print(f"warning: {label} has no GitHub remote; links omitted", file=sys.stderr)
    print(f"script_map_gitinfo: {len(files)} files, {len(repos)} repos -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
