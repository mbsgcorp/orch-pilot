#!/usr/bin/env python3
"""Cache a short AI summary of every SuiteScript file in one or more repos.

Usage: python3 tools/script_map_summarize.py --repo LABEL=PATH [--repo LABEL=PATH ...]
                                             --cache FILE [--model NAME] [--max-chars N]

Needs ANTHROPIC_API_KEY in the environment. Repo arguments and branch checks
follow tools/script_map.py. Files are found and classified the same way; each
SuiteScript file is hashed (SHA-256 of its bytes) and only files whose hash is
not already in the cache are sent to the model, one request per file. Entries
for files no longer found in a passed repo are pruned; entries for repos not
passed are kept. The cache is written atomically even when some files fail.

The cache is JSON: {"version": 1, "entries": {"LABEL:rel/path.js": {"sha256",
"summary", "model", "generated_at"}}}. tools/script_map.py reads it with
--summaries. One summary line is printed; the exit code is 0, 2 on input
errors, or 3 when any file failed.
"""

import argparse
import hashlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import script_map  # noqa: E402

API_URL = "https://api.anthropic.com/v1/messages"
SYSTEM_PROMPT = (
    "You explain NetSuite SuiteScript files to NetSuite consultants. Reply with one or "
    "two plain-English sentences (max 50 words) saying what the script does and when it "
    "runs, in business terms. No preamble, no markdown, no code.")
DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_MAX_CHARS = 60000
TIMEOUT = 60
RETRY_DELAY = 10
MAX_SUMMARY = 400


class SummaryError(Exception):
    """A model call failed; str(error) is the reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _post(request, timeout):
    """Return the parsed JSON reply, or raise HTTPError/URLError/TimeoutError/SummaryError."""
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SummaryError("invalid JSON response") from None


def _attempt(request, timeout):
    """Return (reply, None) on success or (None, (retryable, reason)) on failure."""
    try:
        return _post(request, timeout), None
    except urllib.error.HTTPError as exc:
        exc.close()
        return None,(exc.code == 429 or 500 <= exc.code <= 599, f"HTTP {exc.code}")
    except (TimeoutError, socket.timeout) as exc:
        return None, (True, str(exc) or "timed out")
    except urllib.error.URLError as exc:
        return None, (True, str(exc.reason))


def call_model(api_key, model, system, user, timeout):
    """POST one Messages API request and return the reply text.

    Retries once after RETRY_DELAY seconds on HTTP 429, HTTP 5xx, a timeout or a
    URL error. Raises SummaryError on any other failure or an empty reply.
    """
    body = json.dumps({
        "model": model,
        "max_tokens": 200,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")
    request = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    reply, failure = _attempt(request, timeout)
    if failure is not None:
        retryable, reason = failure
        if not retryable:
            raise SummaryError(reason)
        time.sleep(RETRY_DELAY)
        reply, failure = _attempt(request, timeout)
        if failure is not None:
            raise SummaryError(failure[1])
    blocks = reply.get("content") if isinstance(reply, dict) else None
    text = "".join(block.get("text", "") for block in blocks or []
                   if isinstance(block, dict) and block.get("type") == "text"
                   and isinstance(block.get("text"), str))
    if not text.strip():
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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Cache AI summaries of SuiteScript files for script_map.py.")
    parser.add_argument("--repo", action="append", required=True,
                        metavar="LABEL=PATH",
                        help="repository to scan; repeat for several repos")
    parser.add_argument("--cache", required=True, help="summary cache JSON file")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"model name (default {DEFAULT_MODEL})")
    parser.add_argument("--max-chars", type=positive_int, default=DEFAULT_MAX_CHARS,
                        help=f"characters of code sent per file (default {DEFAULT_MAX_CHARS})")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
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
            status = script_map.branch_status(path)
        except OSError as exc:
            print(f"error: {label} cannot read {path / '.git' / 'HEAD'}: {exc.strerror}",
                  file=sys.stderr)
            return 2
        if status is None:
            warnings.append(f"warning: {label} branch not verified")
        elif status != "main":
            print(f"error: {label} is on {status}, not main", file=sys.stderr)
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

    cached = new = failed = 0
    found = set()
    for label, root in repos:
        walk_errors = []
        for rel, full in script_map.js_files(root, walk_errors):
            key = f"{label}:{rel}"
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
            entry = entries.get(key)
            if isinstance(entry, dict) and entry.get("sha256") == sha:
                cached += 1
                continue
            user = f"File: {rel}\nScript type: {script_type}\n\n{text[:args.max_chars]}"
            try:
                reply = call_model(api_key, args.model, SYSTEM_PROMPT, user, TIMEOUT)
            except SummaryError as exc:
                print(f"warning: {key} summary failed: {exc.reason}", file=sys.stderr)
                failed += 1
                continue
            entries[key] = {
                "sha256": sha,
                "summary": " ".join(reply.split())[:MAX_SUMMARY],
                "model": args.model,
                "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            new += 1
        for exc in walk_errors:
            try:
                rel = Path(exc.filename).relative_to(root).as_posix()
            except (TypeError, ValueError):
                rel = str(exc.filename)
            print(f"warning: {label}:{rel} summary failed: unreadable: {exc.strerror}",
                  file=sys.stderr)
            failed += 1

    prefixes = tuple(f"{label}:" for label, _ in repos)
    stale = [key for key in entries if key.startswith(prefixes) and key not in found]
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

    print(f"script_map_summarize: {cached} cached, {new} new, {failed} failed, "
          f"{len(stale)} pruned -> {args.cache}")
    return 3 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
