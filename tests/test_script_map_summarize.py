import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import script_map  # noqa: E402
import script_map_summarize  # noqa: E402

SCRIPT = TOOLS_DIR / "script_map_summarize.py"

USER_EVENT = """/**
 * @NApiVersion 2.1
 * @NScriptType UserEventScript
 */
define(['N/record'], (record) => {
    const beforeLoad = (ctx) => {};
    return { beforeLoad };
});
"""

SUITELET = "/** @NApiVersion 2.1\n * @NScriptType Suitelet\n */\ndefine([], () => ({}));\n"

FAKE_KEY = "test-placeholder"


class FakeResponse:
    def __init__(self, payload):
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code):
    return urllib.error.HTTPError("x", code, "x", {}, None)


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cache = self.root / "cache" / "summaries.json"
        # Fail any test that would reach the network.
        mock.patch("urllib.request.urlopen",
                   side_effect=AssertionError("network call attempted")).start()
        self.sleep = mock.patch("time.sleep").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self._tmp.cleanup()

    def make_repo(self, name, files=None):
        repo = self.root / name
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        for rel, text in (files or {}).items():
            self.write(repo, rel, text)
        return repo

    def write(self, repo, rel, text):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return path

    def sha(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def write_cache(self, entries):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")

    def read_cache(self):
        return json.loads(self.cache.read_text(encoding="utf-8"))

    def run_main(self, argv, reply="A summary.", key=FAKE_KEY):
        out = io.StringIO()
        err = io.StringIO()
        env = {"ANTHROPIC_API_KEY": key} if key is not None else {}
        model = mock.patch.object(script_map_summarize, "call_model")
        with mock.patch.dict(os.environ, env, clear=False) as _, model as call, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if key is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            if isinstance(reply, list):
                call.side_effect = reply
            elif isinstance(reply, Exception):
                call.side_effect = reply
            else:
                call.return_value = reply
            code = script_map_summarize.main(list(argv))
        return code, out.getvalue(), err.getvalue(), call

    def args(self, *repos):
        argv = []
        for label, path in repos:
            argv += ["--repo", f"{label}={path}"]
        return argv + ["--cache", str(self.cache)]

    # CLI

    def test_help_exits_zero(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("--repo", "--cache", "--model", "--max-chars"):
            self.assertIn(flag, result.stdout)

    def test_defaults(self):
        self.assertEqual(script_map_summarize.DEFAULT_MODEL, "claude-haiku-5-5")
        self.assertEqual(script_map_summarize.DEFAULT_MAX_CHARS, 60000)
        self.assertEqual(script_map_summarize.SYSTEM_PROMPT,
                         "You explain NetSuite SuiteScript files to NetSuite consultants. "
                         "Reply with one or two plain-English sentences (max 50 words) saying "
                         "what the script does and when it runs, in business terms. "
                         "No preamble, no markdown, no code.")

    def test_missing_required_args(self):
        code, out, err, _ = self.run_main([])
        self.assertEqual(code, 2)
        self.assertIn("--repo", err)

    def test_missing_api_key(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        for key in (None, ""):
            with self.subTest(key=key):
                code, out, err, call = self.run_main(self.args(("A", repo)), key=key)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(err.strip(), "error: ANTHROPIC_API_KEY is not set")
                self.assertFalse(self.cache.exists())
                call.assert_not_called()

    def test_missing_api_key_leaves_existing_cache(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text("{broken", encoding="utf-8")
        code, _, err, _ = self.run_main(self.args(("A", repo)), key=None)
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "error: ANTHROPIC_API_KEY is not set")
        self.assertEqual(self.cache.read_text(encoding="utf-8"), "{broken")

    def test_missing_api_key_checked_before_repos(self):
        code, _, err, _ = self.run_main(["--repo", "nolabel", "--cache", str(self.cache)],
                                        key=None)
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "error: ANTHROPIC_API_KEY is not set")

    def test_repo_validation_matches_script_map(self):
        good = self.make_repo("a")
        wrong = self.make_repo("b")
        (wrong / ".git" / "HEAD").write_text("ref: refs/heads/sandbox\n", encoding="utf-8")
        afile = self.write(self.root, "f.txt", "x")
        cases = [
            (["--repo", "nolabel"], "LABEL=PATH"),
            (["--repo", f"={good}"], "empty label"),
            (["--repo", f"A={good}", "--repo", f"A={good}"], "duplicate repo label 'A'"),
            (["--repo", f"A={self.root / 'nope'}"], "does not exist"),
            (["--repo", f"A={afile}"], "is not a directory"),
            (["--repo", f"B={wrong}"], "error: B is on sandbox, not main"),
        ]
        for argv, message in cases:
            with self.subTest(message=message):
                code, out, err, call = self.run_main(argv + ["--cache", str(self.cache)])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn(message, err)
                self.assertFalse(self.cache.exists())
                call.assert_not_called()

    def test_no_git_warns(self):
        repo = self.root / "plain"
        repo.mkdir()
        code, out, err, _ = self.run_main(self.args(("A", repo)))
        self.assertEqual(code, 0)
        self.assertEqual(err.strip(), "warning: A branch not verified")

    # Cache file

    def test_invalid_cache_left_unchanged(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        self.cache.parent.mkdir(parents=True)
        for content in ("{not json", json.dumps({"version": 2, "entries": {}}),
                        json.dumps({"version": 1}), json.dumps({"version": 1, "entries": []}),
                        json.dumps([1, 2])):
            with self.subTest(content=content):
                self.cache.write_text(content, encoding="utf-8")
                code, out, err, call = self.run_main(self.args(("A", repo)))
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(err.strip(),
                                 f"error: {self.cache} is not a valid summary cache")
                self.assertEqual(self.cache.read_text(encoding="utf-8"), content)
                call.assert_not_called()

    def test_missing_cache_starts_empty_and_creates_parents(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        code, out, err, call = self.run_main(self.args(("A", repo)))
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(call.call_count, 1)
        cache = self.read_cache()
        self.assertEqual(cache["version"], 1)
        self.assertEqual(list(cache["entries"]), ["A:ue.js"])
        self.assertFalse(Path(str(self.cache) + ".tmp").exists())

    def test_new_and_changed_files_get_one_call_each(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT, "UserEvent/new.js": SUITELET,
                                    "same.js": SUITELET, "plain.js": "console.log(1);\n"})
        changed = repo / "ue.js"
        same = repo / "same.js"
        self.write_cache({
            "A:ue.js": {"sha256": "0" * 64, "summary": "Old.", "model": "old",
                        "generated_at": "2026-01-01T00:00:00Z"},
            "A:same.js": {"sha256": self.sha(same), "summary": "Kept.", "model": "old",
                          "generated_at": "2026-01-01T00:00:00Z"},
        })
        code, out, err, call = self.run_main(
            self.args(("A", repo)) + ["--model", "my-model"],
            reply=["New for ue.js.", "New for new.js."])
        self.assertEqual(code, 0, err)
        self.assertEqual(call.call_count, 2)
        users = [c.args[3] for c in call.call_args_list]
        self.assertTrue(users[0].startswith("File: ue.js\nScript type: UserEventScript\n\n"))
        self.assertTrue(users[1].startswith("File: UserEvent/new.js\nScript type: Suitelet\n\n"))
        for c in call.call_args_list:
            self.assertEqual(c.args[0], FAKE_KEY)
            self.assertEqual(c.args[1], "my-model")
            self.assertEqual(c.args[2], script_map_summarize.SYSTEM_PROMPT)
            self.assertEqual(c.args[4], 60)
        entries = self.read_cache()["entries"]
        self.assertEqual(set(entries), {"A:ue.js", "A:UserEvent/new.js", "A:same.js"})
        self.assertEqual(entries["A:ue.js"]["sha256"], self.sha(changed))
        self.assertEqual(entries["A:ue.js"]["summary"], "New for ue.js.")
        self.assertEqual(entries["A:ue.js"]["model"], "my-model")
        self.assertRegex(entries["A:ue.js"]["generated_at"],
                         r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(entries["A:UserEvent/new.js"]["sha256"],
                         self.sha(repo / "UserEvent/new.js"))
        self.assertEqual(entries["A:same.js"]["summary"], "Kept.")
        self.assertEqual(out, f"script_map_summarize: 1 cached, 2 new, 0 failed, 0 pruned "
                              f"-> {self.cache}\n")

    def test_cached_hit_makes_no_call(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        entry = {"sha256": self.sha(repo / "ue.js"), "summary": "Kept.", "model": "m",
                 "generated_at": "2026-01-01T00:00:00Z"}
        self.write_cache({"A:ue.js": entry})
        code, out, err, call = self.run_main(self.args(("A", repo)))
        self.assertEqual(code, 0, err)
        call.assert_not_called()
        self.assertEqual(self.read_cache()["entries"], {"A:ue.js": entry})
        self.assertEqual(out, f"script_map_summarize: 1 cached, 0 new, 0 failed, 0 pruned "
                              f"-> {self.cache}\n")

    def test_failure_keeps_old_entry_and_writes_others(self):
        repo = self.make_repo("a", {"a.js": USER_EVENT, "b.js": SUITELET})
        old = {"sha256": "0" * 64, "summary": "Old.", "model": "m",
               "generated_at": "2026-01-01T00:00:00Z"}
        self.write_cache({"A:a.js": old})
        code, out, err, call = self.run_main(
            self.args(("A", repo)),
            reply=[script_map_summarize.SummaryError("HTTP 500"), "B summary."])
        self.assertEqual(code, 3)
        self.assertEqual(err.strip(), "warning: A:a.js summary failed: HTTP 500")
        entries = self.read_cache()["entries"]
        self.assertEqual(entries["A:a.js"], old)
        self.assertEqual(entries["A:b.js"]["summary"], "B summary.")
        self.assertEqual(out, f"script_map_summarize: 0 cached, 1 new, 1 failed, 0 pruned "
                              f"-> {self.cache}\n")

    def test_failure_without_old_entry_adds_nothing(self):
        repo = self.make_repo("a", {"a.js": USER_EVENT})
        code, out, err, _ = self.run_main(
            self.args(("A", repo)), reply=script_map_summarize.SummaryError("empty response"))
        self.assertEqual(code, 3)
        self.assertIn("warning: A:a.js summary failed: empty response", err)
        self.assertEqual(self.read_cache(), {"version": 1, "entries": {}})

    def test_prune_passed_label_keep_other_label(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        entry = {"sha256": self.sha(repo / "ue.js"), "summary": "Kept.", "model": "m",
                 "generated_at": "2026-01-01T00:00:00Z"}
        gone = dict(entry, sha256="1" * 64)
        self.write_cache({"A:ue.js": entry, "A:removed.js": gone, "B:removed.js": gone,
                          "AB:removed.js": gone})
        code, out, err, call = self.run_main(self.args(("A", repo)))
        self.assertEqual(code, 0, err)
        call.assert_not_called()
        self.assertEqual(set(self.read_cache()["entries"]),
                         {"A:ue.js", "B:removed.js", "AB:removed.js"})
        self.assertEqual(out, f"script_map_summarize: 1 cached, 0 new, 0 failed, 1 pruned "
                              f"-> {self.cache}\n")

    def test_too_large_file_skipped_not_failed_or_pruned(self):
        repo = self.make_repo("a", {"big.js": "// @NApiVersion 2.1\n" + "x" * 50})
        old = {"sha256": "0" * 64, "summary": "Old.", "model": "m",
               "generated_at": "2026-01-01T00:00:00Z"}
        self.write_cache({"A:big.js": old})
        with mock.patch.object(script_map, "MAX_BYTES", 10):
            code, out, err, call = self.run_main(self.args(("A", repo)))
        self.assertEqual(code, 0)
        call.assert_not_called()
        self.assertEqual(err.strip(), "warning: A:big.js skipped: too large")
        self.assertEqual(self.read_cache()["entries"], {"A:big.js": old})
        self.assertIn("0 cached, 0 new, 0 failed, 0 pruned", out)

    def test_excluded_and_non_suitescript_files_not_sent(self):
        repo = self.make_repo("a", {"plain.js": "x();\n", "tests/t.js": USER_EVENT,
                                    "x.min.js": USER_EVENT})
        code, out, _, call = self.run_main(self.args(("A", repo)))
        self.assertEqual(code, 0)
        call.assert_not_called()
        self.assertIn("0 cached, 0 new, 0 failed, 0 pruned", out)

    def test_summary_whitespace_collapsed_and_truncated(self):
        repo = self.make_repo("a", {"a.js": USER_EVENT, "b.js": SUITELET})
        long_reply = "word " * 200
        code, _, err, _ = self.run_main(
            self.args(("A", repo)), reply=["  Holds\n\n orders\twhen   low. \n", long_reply])
        self.assertEqual(code, 0, err)
        entries = self.read_cache()["entries"]
        self.assertEqual(entries["A:a.js"]["summary"], "Holds orders when low.")
        self.assertEqual(entries["A:b.js"]["summary"], " ".join(long_reply.split())[:400])
        self.assertEqual(len(entries["A:b.js"]["summary"]), 400)

    def test_max_chars_truncates_code_sent(self):
        body = USER_EVENT + "// " + "y" * 500 + "\n"
        repo = self.make_repo("a", {"ue.js": body})
        code, _, err, call = self.run_main(self.args(("A", repo)) + ["--max-chars", "50"])
        self.assertEqual(code, 0, err)
        user = call.call_args.args[3]
        prefix = "File: ue.js\nScript type: UserEventScript\n\n"
        self.assertTrue(user.startswith(prefix))
        self.assertEqual(user[len(prefix):], body[:50])
        self.assertEqual(len(user[len(prefix):]), 50)

    def test_default_max_chars(self):
        body = USER_EVENT + "// " + "y" * 70000 + "\n"
        repo = self.make_repo("a", {"ue.js": body})
        code, _, _, call = self.run_main(self.args(("A", repo)))
        self.assertEqual(code, 0)
        code_part = call.call_args.args[3].split("\n\n", 1)[1]
        self.assertEqual(len(code_part), 60000)

    def test_crlf_and_invalid_utf8_still_summarized(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT.replace("\n", "\r\n").encode()
                                    + b"// caf\xe9\r\n"})
        code, _, err, call = self.run_main(self.args(("A", repo)))
        self.assertEqual(code, 0, err)
        user = call.call_args.args[3]
        self.assertNotIn("\r", user)
        self.assertIn("caf\ufffd", user)
        self.assertEqual(self.read_cache()["entries"]["A:ue.js"]["sha256"],
                         self.sha(repo / "ue.js"))

    def test_two_repos_and_subprocess_without_network(self):
        a = self.make_repo("a", {"ue.js": USER_EVENT})
        b = self.make_repo("b", {"sl.js": SUITELET})
        code, out, err, call = self.run_main(self.args(("A", a), ("B", b)),
                                             reply=["A one.", "B one."])
        self.assertEqual(code, 0, err)
        self.assertEqual(set(self.read_cache()["entries"]), {"A:ue.js", "B:sl.js"})
        # The cache feeds script_map --summaries.
        out_html = self.root / "map.html"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(script_map.main(["--repo", f"A={a}", "--out", str(out_html),
                                              "--summaries", str(self.cache)]), 0)
        self.assertIn('"summary_source": "ai"', out_html.read_text(encoding="utf-8"))

    # call_model

    def call(self):
        return script_map_summarize.call_model(FAKE_KEY, "m", "sys", "user text", 60)

    def test_call_model_request_shape(self):
        reply = {"content": [{"type": "text", "text": "Hello "},
                             {"type": "tool_use", "text": "ignored"},
                             {"type": "text", "text": "world."}]}
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(reply)) as urlopen:
            self.assertEqual(self.call(), "Hello world.")
        request = urlopen.call_args.args[0]
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 60})
        self.assertEqual(request.full_url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.get_method(), "POST")
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertEqual(headers["x-api-key"], FAKE_KEY)
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {
            "model": "m", "max_tokens": 200, "system": "sys",
            "messages": [{"role": "user", "content": "user text"}]})
        self.sleep.assert_not_called()

    def test_call_model_retries_once_on_429(self):
        ok = FakeResponse({"content": [{"type": "text", "text": "Fine."}]})
        with mock.patch("urllib.request.urlopen", side_effect=[http_error(429), ok]) as urlopen:
            self.assertEqual(self.call(), "Fine.")
        self.assertEqual(urlopen.call_count, 2)
        self.sleep.assert_called_once_with(10)

    def test_call_model_second_failure_raises(self):
        cases = [
            ([http_error(503), http_error(502)], "HTTP 502"),
            ([urllib.error.URLError("refused"), urllib.error.URLError("still refused")],
             "still refused"),
            ([TimeoutError("timed out"), TimeoutError("timed out again")], "timed out again"),
        ]
        for errors, reason in cases:
            with self.subTest(reason=reason):
                self.sleep.reset_mock()
                with mock.patch("urllib.request.urlopen", side_effect=errors) as urlopen:
                    with self.assertRaises(script_map_summarize.SummaryError) as ctx:
                        self.call()
                self.assertEqual(str(ctx.exception), reason)
                self.assertEqual(urlopen.call_count, 2)
                self.sleep.assert_called_once_with(10)

    def test_call_model_400_fails_without_retry(self):
        with mock.patch("urllib.request.urlopen", side_effect=[http_error(400)]) as urlopen:
            with self.assertRaises(script_map_summarize.SummaryError) as ctx:
                self.call()
        self.assertEqual(str(ctx.exception), "HTTP 400")
        self.assertEqual(urlopen.call_count, 1)
        self.sleep.assert_not_called()

    def test_call_model_empty_and_invalid_responses(self):
        for payload, reason in (({"content": []}, "empty response"),
                                ({"content": [{"type": "text", "text": "  \n"}]},
                                 "empty response"),
                                ({"error": "x"}, "empty response"),
                                (b"not json", "invalid JSON response")):
            with self.subTest(reason=reason, payload=payload):
                with mock.patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
                    with self.assertRaises(script_map_summarize.SummaryError) as ctx:
                        self.call()
                self.assertEqual(str(ctx.exception), reason)
        self.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
