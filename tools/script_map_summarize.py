#!/usr/bin/env python3
r"""Cache a short AI summary of every SuiteScript file in one or more repos.

Usage: python3 tools/script_map_summarize.py --repo LABEL=PATH [--repo LABEL=PATH ...]
                                             --cache FILE --command "CMD" [--max-chars N]
                                             [--limit N] [--timeout SECONDS]
                                             [--branch NAME] [--namespace NAME]
                                             [--force KEY ...] [--notes FILE]

Summaries come from a local command (for example a subscription CLI such as
Codex): --command is split like a shell would split it, then run without a
shell once per file. The prompt is written to its stdin and the summary is read
from its stdout; stderr is only used in failure messages. A failed or timed-out
run is retried once after 10 seconds. --timeout (default 300) is the limit in
seconds for each run. --limit N stops after N new summaries in this run (0, the
default, means no limit); the remaining files are counted as deferred and picked
up by the next run. After 3 consecutive failed files no further calls are made
and the remaining files are counted as failed.

Example:
    python3 tools/script_map_summarize.py --repo RTMH=../rtmh --cache summaries.json \
        --command "codex exec --skip-git-repo-check -" --limit 50

Repo arguments and branch checks follow tools/script_map.py; --branch (default
main) is the branch every repo's HEAD must name. Files are found and classified
the same way; each SuiteScript file is hashed (SHA-256 of its bytes) and only
files whose hash is not already in the cache under their own key are
considered. If any other cache entry (any key, any namespace) has the same
hash, its summary and model are copied to the file's key and counted as reused,
with no command run; reused files do not count toward --limit or the failure
streak. The remaining files are sent to the command, one run per file. Entries
for files no longer found in a passed repo are pruned, within the run's
namespace only; entries for repos not passed and entries in other namespaces
are kept. The cache is written atomically even when some files fail.

The cache is JSON: {"version": 1, "entries": {"[NAME|]LABEL:rel/path.js":
{"sha256", "summary", "model", "generated_at", "notes_sha"}}}; "model" holds the
--command string and "notes_sha" the notes fingerprint the summary was made with
("" when none; a missing field counts as ""). The "NAME|" prefix is present only
when --namespace NAME is given (for example "sandbox|RTMH:x.js"), so prod and
sandbox runs keep separate entries.

--notes FILE reads the correction notes file kept by the site's rebuild
function: {"version": 1, "notes": {"LABEL:path": [{"id", "text", "author",
"created_at", "cleared_at", "cleared_by", "clear_reason"}, ...]}}. A file's notes
are those under its cache key without the "NAME|" prefix; a note is active when
cleared_at is null, and malformed notes are ignored. Active notes (oldest first)
are appended to the prompt under a "Reviewer notes from the MBSG team" heading.
The notes fingerprint is "" with no active notes, else the SHA-256 of the JSON
list of sorted active note ids. A file cached for the same sha256 but a
different notes_sha is regenerated: the command is always run, it does not count
toward --limit but does count toward the failure streak, and on success only its
own entry is replaced (counted as new). A file with active notes is never filled
by hash reuse, and a summary made with notes is never reused for another key. A
missing or invalid notes file is an input error (exit 2).

--force KEY (repeatable) regenerates the summary for that cache key even when
it is cached: the command is always run for it (no cache or hash reuse), it
does not count toward --limit but does count toward the failure streak. On
success the entry is replaced, counted as new, and every other entry (any key,
any namespace) with the same sha256 and the same notes fingerprint gets the same
summary, model, generated_at and notes_sha. The key's active notes are included
in the prompt. On failure the old entry is kept and the file counts as failed.
Keys for another namespace than --namespace are ignored; a key in this
namespace that matches no file found in this run prints
"warning: --force key not found: KEY" to stderr and is not a failure.
tools/script_map.py reads it with --summaries. One line is printed:
"script_map_summarize: C cached, R reused, N new, F failed, D deferred, P pruned -> FILE".
The exit code is 0 (deferred files alone do not fail a run), 2 on usage, input
or cache errors, or 3 when any file failed.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import script_map  # noqa: E402

SYSTEM_PROMPT = (
    "You explain NetSuite SuiteScript files to NetSuite consultants. Reply with one or "
    "two plain-English sentences (max 50 words) saying what the script does and when it "
    "runs, in business terms. No preamble, no markdown, no code.")
DEFAULT_MAX_CHARS = 60000
DEFAULT_TIMEOUT = 300
RETRY_DELAY = 10
MAX_SUMMARY = 400
MAX_FAILURE_STREAK = 3
MAX_STDERR = 200
EXAMPLE_COMMAND = "codex exec --skip-git-repo-check -"
NAMESPACE_RE = re.compile(r"[A-Za-z0-9_-]*")
NOTES_HEADER = ("Reviewer notes from the MBSG team about this script. Check each note "
                "against the code; if the code contradicts a note, follow the code:")


class SummaryError(Exception):
    """A model call failed; str(error) is the reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _run(argv, prompt, timeout):
    """Return (completed, None) on exit 0 or (None, reason) on a failed, timed-out or
    unstartable run."""
    try:
        result = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                                timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except OSError as exc:
        return None, f"could not run: {(exc.strerror or str(exc))[:MAX_STDERR]}"
    if result.returncode != 0:
        lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
        if lines:
            return None, f"exit {result.returncode}: {lines[-1][:MAX_STDERR]}"
        return None, f"exit {result.returncode}"
    return result, None


def call_command(argv, prompt, timeout):
    """Run argv without a shell, prompt on stdin, and return its stdout stripped.

    Retries once after RETRY_DELAY seconds on a non-zero exit, a timeout or an
    OSError starting the command.
    Raises SummaryError with the last attempt's reason, or "empty response"
    when stdout is blank.
    """
    result, reason = _run(argv, prompt, timeout)
    if reason is not None:
        time.sleep(RETRY_DELAY)
        result, reason = _run(argv, prompt, timeout)
        if reason is not None:
            raise SummaryError(reason)
    text = (result.stdout or "").strip()
    if not text:
        raise SummaryError("empty response")
    return text


def positive_int(value):
    """argparse type: an integer of at least 1."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be at least 1") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def non_negative_int(value):
    """argparse type: an integer of at least 0."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be at least 0") from None
    if number < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Cache AI summaries of SuiteScript files for script_map.py.\n"
                    "Each file's prompt is piped to a local command on stdin, run without a\n"
                    "shell; the summary is read from its stdout.",
        epilog=f'example:\n  python3 tools/script_map_summarize.py --repo A=../repo '
               f'--cache summaries.json \\\n      --command "{EXAMPLE_COMMAND}" --limit 50\n\n'
               "prints: script_map_summarize: C cached, R reused, N new, F failed, "
               "D deferred, P pruned -> FILE\n"
               "exit codes: 0 ok (deferred files alone are ok), 2 usage/input/cache error, "
               "3 some files failed",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", action="append", required=True,
                        metavar="LABEL=PATH",
                        help="repository to scan; repeat for several repos")
    parser.add_argument("--cache", required=True, help="summary cache JSON file")
    parser.add_argument("--command", required=True, metavar="CMD",
                        help="local command that reads the prompt on stdin and prints the "
                             "summary on stdout; split like a shell would, run without a "
                             "shell; stored as the cache 'model' field")
    parser.add_argument("--max-chars", type=positive_int, default=DEFAULT_MAX_CHARS,
                        help=f"characters of code sent per file (default {DEFAULT_MAX_CHARS})")
    parser.add_argument("--limit", type=non_negative_int, default=0, metavar="N",
                        help="stop after N new summaries this run; remaining files are "
                             "deferred to the next run (default 0, no limit)")
    parser.add_argument("--timeout", type=positive_int, default=DEFAULT_TIMEOUT,
                        metavar="SECONDS",
                        help=f"seconds allowed per command run (default {DEFAULT_TIMEOUT})")
    parser.add_argument("--branch", default="main", metavar="NAME",
                        help="branch every repo's HEAD must name (default main)")
    parser.add_argument("--namespace", default="", metavar="NAME",
                        help="cache key prefix (keys become NAME|LABEL:path) so runs for "
                             "different branches keep separate entries (default none)")
    parser.add_argument("--force", action="append", default=[], metavar="KEY",
                        help="cache key ([NAME|]LABEL:path) to regenerate even when cached; "
                             "repeatable; keys for other namespaces are ignored")
    parser.add_argument("--notes", default=None, metavar="FILE",
                        help="correction notes file (summary-notes.json); active notes are "
                             "added to the prompt and a changed set of notes regenerates "
                             "the summary")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if not script_map.BRANCH_RE.fullmatch(args.branch):
        print("error: --branch must be a branch name", file=sys.stderr)
        return 2
    if not NAMESPACE_RE.fullmatch(args.namespace):
        print("error: --namespace must be letters, digits, _ or -", file=sys.stderr)
        return 2

    try:
        command_argv = shlex.split(args.command)
    except ValueError as exc:
        print(f"error: --command: {exc}", file=sys.stderr)
        return 2
    if not command_argv:
        print("error: --command is empty", file=sys.stderr)
        return 2
    if shutil.which(command_argv[0]) is None:
        print(f"error: command not found: {command_argv[0]}", file=sys.stderr)
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

    warnings = []
    for label, path in repos:
        try:
            status = script_map.branch_status(path, args.branch)
        except OSError as exc:
            print(f"error: {label} cannot read {path / '.git' / 'HEAD'}: {exc.strerror}",
                  file=sys.stderr)
            return 2
        if status is None:
            warnings.append(f"warning: {label} branch not verified")
        elif status != args.branch:
            print(f"error: {label} is on {status}, not {args.branch}", file=sys.stderr)
            return 2
    for warning in warnings:
        print(warning, file=sys.stderr)

    cache_path = Path(args.cache)
    try:
        cache = script_map.load_summary_cache(cache_path)
    except FileNotFoundError:
        cache = {"version": 1, "entries": {}}
    except (OSError, ValueError):
        print(f"error: {args.cache} is not a valid summary cache", file=sys.stderr)
        return 2
    entries = cache["entries"]
    ns_prefix = f"{args.namespace}|" if args.namespace else ""

    notes = {}
    if args.notes is not None:
        try:
            notes = script_map.load_notes(args.notes)
        except FileNotFoundError:
            print(f"error: {args.notes} does not exist", file=sys.stderr)
            return 2
        except (OSError, ValueError):
            print(f"error: {args.notes} is not a valid notes file", file=sys.stderr)
            return 2

    def active_notes(key):
        found_notes = [note for note in
                       script_map.normalize_notes(notes.get(script_map.notes_id(key)))
                       if note["cleared_at"] is None]
        return sorted(found_notes, key=lambda note: note["created_at"])

    def fingerprint(active):
        if not active:
            return ""
        ids = sorted(note["id"] for note in active)
        return hashlib.sha256(json.dumps(ids).encode("utf-8")).hexdigest()

    def now():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def reusable(value):
        return (isinstance(value, dict) and isinstance(value.get("sha256"), str)
                and isinstance(value.get("summary"), str)
                and value.get("notes_sha", "") == "")

    by_sha = {}
    for key in sorted(entries):
        value = entries[key]
        if reusable(value):
            by_sha.setdefault(value["sha256"], value)

    def key_namespace(key):
        head = key.split(":", 1)[0]
        return head.split("|", 1)[0] if "|" in head else ""

    forced = {key for key in args.force if key_namespace(key) == args.namespace}

    cached = reused = new = failed = deferred = not_attempted = streak = 0
    forced_new = notes_new = 0
    found = set()
    for label, root in repos:
        walk_errors = []
        for rel, full in script_map.js_files(root, walk_errors):
            key = f"{ns_prefix}{label}:{rel}"
            found.add(key)
            try:
                if os.path.getsize(full) > script_map.MAX_BYTES:
                    print(f"warning: {key} skipped: too large", file=sys.stderr)
                    continue
                blob = full.read_bytes()
            except OSError as exc:
                print(f"warning: {key} summary failed: unreadable: {exc.strerror}",
                      file=sys.stderr)
                failed += 1
                continue
            sha = hashlib.sha256(blob).hexdigest()
            text = blob.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            is_script, script_type, _ = script_map.classify(text)
            if not is_script:
                continue
            force = key in forced
            active = active_notes(key)
            fp = fingerprint(active)
            entry = entries.get(key)
            notes_driven = False
            if not force and isinstance(entry, dict) and entry.get("sha256") == sha:
                if entry.get("notes_sha", "") == fp:
                    cached += 1
                    continue
                notes_driven = True
            source = None if force or notes_driven or fp else by_sha.get(sha)
            if source is not None:
                entries[key] = {
                    "sha256": sha,
                    "summary": source["summary"],
                    "model": source.get("model", ""),
                    "generated_at": now(),
                    "notes_sha": "",
                }
                reused += 1
                continue
            if (not force and not notes_driven and args.limit
                    and new - forced_new - notes_new >= args.limit):
                deferred += 1
                continue
            if streak >= MAX_FAILURE_STREAK:
                failed += 1
                not_attempted += 1
                continue
            user = f"File: {rel}\nScript type: {script_type}\n\n{text[:args.max_chars]}"
            prompt = f"{SYSTEM_PROMPT}\n\n{user}"
            if active:
                prompt += "\n\n" + NOTES_HEADER + "\n" + "\n".join(
                    "- " + note["text"].replace("\n", " ") for note in active)
            try:
                reply = call_command(command_argv, prompt, args.timeout)
            except SummaryError as exc:
                print(f"warning: {key} summary failed: {exc.reason}", file=sys.stderr)
                failed += 1
                streak += 1
                continue
            entries[key] = {
                "sha256": sha,
                "summary": " ".join(reply.split())[:MAX_SUMMARY],
                "model": args.command,
                "generated_at": now(),
                "notes_sha": fp,
            }
            if force:
                fresh = entries[key]
                for other, value in entries.items():
                    if (other != key and isinstance(value, dict) and value.get("sha256") == sha
                            and fingerprint(active_notes(other)) == fp):
                        for field in ("summary", "model", "generated_at", "notes_sha"):
                            value[field] = fresh[field]
                if fp == "":
                    by_sha[sha] = fresh
                forced_new += 1
            elif notes_driven:
                notes_new += 1
            elif fp == "":
                by_sha.setdefault(sha, entries[key])
            new += 1
            streak = 0
        for exc in walk_errors:
            try:
                rel = Path(exc.filename).relative_to(root).as_posix()
            except (TypeError, ValueError):
                rel = str(exc.filename)
            print(f"warning: {label}:{rel} summary failed: unreadable: {exc.strerror}",
                  file=sys.stderr)
            failed += 1

    for key in sorted(forced - found):
        print(f"warning: --force key not found: {key}", file=sys.stderr)

    if not_attempted:
        print(f"warning: stopping after {MAX_FAILURE_STREAK} consecutive failures; "
              f"{not_attempted} files not attempted", file=sys.stderr)

    def in_namespace(key):
        if ns_prefix:
            return key.startswith(ns_prefix)
        return "|" not in key.split(":", 1)[0]

    prefixes = tuple(f"{ns_prefix}{label}:" for label, _ in repos)
    stale = [key for key in entries
             if in_namespace(key) and key.startswith(prefixes) and key not in found]
    for key in stale:
        del entries[key]

    tmp = cache_path.with_name(cache_path.name + ".tmp")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, cache_path)
    except OSError as exc:
        print(f"error: cannot write {args.cache}: {exc}", file=sys.stderr)
        return 2

    print(f"script_map_summarize: {cached} cached, {reused} reused, {new} new, "
          f"{failed} failed, {deferred} deferred, {len(stale)} pruned -> {args.cache}")
    return 3 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
