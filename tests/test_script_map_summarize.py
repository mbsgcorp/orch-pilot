import contextlib
import hashlib
import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
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

COMMAND = "fake-cmd -"
FAKE_PATH = "/usr/bin/fake-cmd"


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

    def run_main(self, argv, reply="A summary.", which=FAKE_PATH):
        out = io.StringIO()
        err = io.StringIO()
        command = mock.patch.object(script_map_summarize, "call_command")
        which_patch = mock.patch("shutil.which", return_value=which)
        with command as call, which_patch as self.which, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if isinstance(reply, list):
                call.side_effect = reply
            elif isinstance(reply, Exception):
                call.side_effect = reply
            else:
                call.return_value = reply
            code = script_map_summarize.main(list(argv))
        return code, out.getvalue(), err.getvalue(), call

    def args(self, *repos, command=COMMAND):
        argv = []
        for label, path in repos:
            argv += ["--repo", f"{label}={path}"]
        return argv + ["--cache", str(self.cache), "--command", command]

    def prompt_user(self, call):
        """Return the user part of a call_command prompt, checking the system prefix."""
        prompt = call.args[1]
        prefix = script_map_summarize.SYSTEM_PROMPT + "\n\n"
        self.assertTrue(prompt.startswith(prefix), prompt[:200])
        return prompt[len(prefix):]

    def entry(self, sha, summary="Old.", model="m"):
        return {"sha256": sha, "summary": summary, "model": model,
                "generated_at": "2026-01-01T00:00:00Z"}

    # CLI

    def test_help_exits_zero(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("--repo", "--cache", "--command", "--max-chars", "--limit", "--timeout"):
            self.assertIn(flag, result.stdout)
        self.assertNotIn("--model", result.stdout)
        self.assertIn('--command "codex exec --skip-git-repo-check -"', result.stdout)

    def test_defaults(self):
        self.assertFalse(hasattr(script_map_summarize, "DEFAULT_MODEL"))
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

    def test_max_chars_must_be_positive(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        for value in ("0", "-5"):
            with self.subTest(value=value):
                code, out, err, call = self.run_main(
                    self.args(("A", repo)) + ["--max-chars", value])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--max-chars", err)
                call.assert_not_called()
                self.assertFalse(self.cache.exists())

    def test_command_is_required(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        code, out, err, call = self.run_main(["--repo", f"A={repo}", "--cache", str(self.cache)])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--command", err)
        call.assert_not_called()
        self.assertFalse(self.cache.exists())

    def test_limit_must_be_non_negative_int(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        for value in ("-1", "x", "1.5"):
            with self.subTest(value=value):
                code, out, err, call = self.run_main(self.args(("A", repo)) + ["--limit", value])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--limit", err)
                call.assert_not_called()
                self.assertFalse(self.cache.exists())

    def test_timeout_must_be_positive(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        for value in ("0", "-3", "x"):
            with self.subTest(value=value):
                code, out, err, call = self.run_main(
                    self.args(("A", repo)) + ["--timeout", value])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--timeout", err)
                call.assert_not_called()
                self.assertFalse(self.cache.exists())

    def test_timeout_passed_through(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        code, _, err, call = self.run_main(self.args(("A", repo)) + ["--timeout", "42"])
        self.assertEqual(code, 0, err)
        self.assertEqual(call.call_args.args[2], 42)

    def test_empty_command(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        for command in ("", "   "):
            with self.subTest(command=command):
                code, out, err, call = self.run_main(self.args(("A", repo), command=command))
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(err, "error: --command is empty\n")
                self.assertFalse(self.cache.exists())
                call.assert_not_called()

    def test_unbalanced_quote_in_command(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        code, out, err, call = self.run_main(self.args(("A", repo), command='codex "exec'))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("error: --command: "), err)
        self.assertFalse(self.cache.exists())
        call.assert_not_called()

    def test_command_not_found(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        code, out, err, call = self.run_main(self.args(("A", repo), command="nope-cmd exec -"),
                                             which=None)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, "error: command not found: nope-cmd\n")
        self.which.assert_called_once_with("nope-cmd")
        self.assertFalse(self.cache.exists())
        call.assert_not_called()

    def test_bad_command_leaves_existing_cache(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text("{broken", encoding="utf-8")
        for command, which, message in (("", FAKE_PATH, "error: --command is empty"),
                                        ("nope-cmd", None, "error: command not found: nope-cmd")):
            with self.subTest(command=command):
                code, _, err, _ = self.run_main(self.args(("A", repo), command=command),
                                                which=which)
                self.assertEqual(code, 2)
                self.assertEqual(err.strip(), message)
                self.assertEqual(self.cache.read_text(encoding="utf-8"), "{broken")

    def test_bad_command_checked_before_repos(self):
        for command, which, message in (("  ", FAKE_PATH, "error: --command is empty"),
                                        ("nope-cmd", None, "error: command not found: nope-cmd")):
            with self.subTest(command=command):
                code, _, err, _ = self.run_main(
                    ["--repo", "nolabel", "--cache", str(self.cache), "--command", command],
                    which=which)
                self.assertEqual(code, 2)
                self.assertEqual(err.strip(), message)

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
                code, out, err, call = self.run_main(
                    argv + ["--cache", str(self.cache), "--command", COMMAND])
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
        command = "codex exec --skip-git-repo-check -"
        code, out, err, call = self.run_main(
            self.args(("A", repo), command=command),
            reply=["New for ue.js.", "New for new.js."])
        self.assertEqual(code, 0, err)
        self.assertEqual(call.call_count, 2)
        users = [self.prompt_user(c) for c in call.call_args_list]
        self.assertEqual(users[0], "File: ue.js\nScript type: UserEventScript\n\n" + USER_EVENT)
        self.assertEqual(users[1], "File: UserEvent/new.js\nScript type: Suitelet\n\n" + SUITELET)
        for c in call.call_args_list:
            self.assertEqual(c.args[0], shlex.split(command))
            self.assertEqual(c.args[2], 300)
        entries = self.read_cache()["entries"]
        self.assertEqual(set(entries), {"A:ue.js", "A:UserEvent/new.js", "A:same.js"})
        self.assertEqual(entries["A:ue.js"]["sha256"], self.sha(changed))
        self.assertEqual(entries["A:ue.js"]["summary"], "New for ue.js.")
        self.assertEqual(entries["A:ue.js"]["model"], command)
        self.assertEqual(entries["A:UserEvent/new.js"]["model"], command)
        self.assertRegex(entries["A:ue.js"]["generated_at"],
                         r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(entries["A:UserEvent/new.js"]["sha256"],
                         self.sha(repo / "UserEvent/new.js"))
        self.assertEqual(entries["A:same.js"]["summary"], "Kept.")
        self.assertEqual(out, f"script_map_summarize: 1 cached, 2 new, 0 failed, 0 deferred, "
                              f"0 pruned "
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
        self.assertEqual(out, f"script_map_summarize: 1 cached, 0 new, 0 failed, 0 deferred, "
                              f"0 pruned "
                              f"-> {self.cache}\n")

    def test_failure_keeps_old_entry_and_writes_others(self):
        repo = self.make_repo("a", {"a.js": USER_EVENT, "b.js": SUITELET})
        old = {"sha256": "0" * 64, "summary": "Old.", "model": "m",
               "generated_at": "2026-01-01T00:00:00Z"}
        self.write_cache({"A:a.js": old})
        code, out, err, call = self.run_main(
            self.args(("A", repo)),
            reply=[script_map_summarize.SummaryError("exit 1: boom"), "B summary."])
        self.assertEqual(code, 3)
        self.assertEqual(err.strip(), "warning: A:a.js summary failed: exit 1: boom")
        entries = self.read_cache()["entries"]
        self.assertEqual(entries["A:a.js"], old)
        self.assertEqual(entries["A:b.js"]["summary"], "B summary.")
        self.assertEqual(out, f"script_map_summarize: 0 cached, 1 new, 1 failed, 0 deferred, "
                              f"0 pruned "
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
        self.assertEqual(out, f"script_map_summarize: 1 cached, 0 new, 0 failed, 0 deferred, "
                              f"1 pruned "
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
        self.assertIn("0 cached, 0 new, 0 failed, 0 deferred, 0 pruned", out)

    def test_excluded_and_non_suitescript_files_not_sent(self):
        repo = self.make_repo("a", {"plain.js": "x();\n", "tests/t.js": USER_EVENT,
                                    "x.min.js": USER_EVENT})
        code, out, _, call = self.run_main(self.args(("A", repo)))
        self.assertEqual(code, 0)
        call.assert_not_called()
        self.assertIn("0 cached, 0 new, 0 failed, 0 deferred, 0 pruned", out)

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
        user = self.prompt_user(call.call_args)
        prefix = "File: ue.js\nScript type: UserEventScript\n\n"
        self.assertTrue(user.startswith(prefix))
        self.assertEqual(user[len(prefix):], body[:50])
        self.assertEqual(len(user[len(prefix):]), 50)

    def test_default_max_chars(self):
        body = USER_EVENT + "// " + "y" * 70000 + "\n"
        repo = self.make_repo("a", {"ue.js": body})
        code, _, _, call = self.run_main(self.args(("A", repo)))
        self.assertEqual(code, 0)
        code_part = self.prompt_user(call.call_args).split("\n\n", 1)[1]
        self.assertEqual(len(code_part), 60000)

    def test_crlf_and_invalid_utf8_still_summarized(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT.replace("\n", "\r\n").encode()
                                    + b"// caf\xe9\r\n"})
        code, _, err, call = self.run_main(self.args(("A", repo)))
        self.assertEqual(code, 0, err)
        user = self.prompt_user(call.call_args)
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

    def test_old_model_value_still_cached(self):
        repo = self.make_repo("a", {"ue.js": USER_EVENT})
        entry = self.entry(self.sha(repo / "ue.js"), "Kept.", model="claude-haiku-5-5")
        self.write_cache({"A:ue.js": entry})
        code, out, err, call = self.run_main(self.args(("A", repo)))
        self.assertEqual(code, 0, err)
        call.assert_not_called()
        self.assertEqual(self.read_cache()["entries"], {"A:ue.js": entry})
        self.assertIn("1 cached, 0 new, 0 failed, 0 deferred, 0 pruned", out)

    # --limit and early stop

    def five_files(self):
        return self.make_repo("a", {f"f{i}.js": USER_EVENT + f"// {i}\n" for i in range(5)})

    def test_limit_defers_remaining_files(self):
        repo = self.five_files()
        old = self.entry("0" * 64)
        self.write_cache({"A:f3.js": old, "A:f4.js": old})
        code, out, err, call = self.run_main(self.args(("A", repo)) + ["--limit", "2"],
                                             reply=["One.", "Two."])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(call.call_count, 2)
        sent = [self.prompt_user(c).split("\n", 1)[0] for c in call.call_args_list]
        self.assertEqual(sent, ["File: f0.js", "File: f1.js"])
        entries = self.read_cache()["entries"]
        self.assertEqual(set(entries), {"A:f0.js", "A:f1.js", "A:f3.js", "A:f4.js"})
        self.assertEqual(entries["A:f3.js"], old)
        self.assertEqual(entries["A:f4.js"], old)
        self.assertEqual(out, f"script_map_summarize: 0 cached, 2 new, 0 failed, 3 deferred, "
                              f"0 pruned -> {self.cache}\n")

    def test_limit_counts_only_successes_and_skips_cached(self):
        repo = self.five_files()
        cached = self.entry(self.sha(repo / "f0.js"), "Kept.")
        self.write_cache({"A:f0.js": cached})
        fail = script_map_summarize.SummaryError("exit 1")
        code, out, err, call = self.run_main(self.args(("A", repo)) + ["--limit", "1"],
                                             reply=[fail, "Two."])
        self.assertEqual(code, 3)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(self.read_cache()["entries"]["A:f0.js"], cached)
        self.assertEqual(out, f"script_map_summarize: 1 cached, 1 new, 1 failed, 2 deferred, "
                              f"0 pruned -> {self.cache}\n")

    def test_limit_zero_is_unlimited(self):
        repo = self.five_files()
        code, out, err, call = self.run_main(self.args(("A", repo)) + ["--limit", "0"])
        self.assertEqual(code, 0, err)
        self.assertEqual(call.call_count, 5)
        self.assertIn("0 cached, 5 new, 0 failed, 0 deferred, 0 pruned", out)

    def test_deferred_files_not_pruned(self):
        repo = self.make_repo("a", {"a.js": USER_EVENT, "b.js": SUITELET})
        old = self.entry("0" * 64)
        self.write_cache({"A:b.js": old, "A:gone.js": old})
        code, out, err, call = self.run_main(self.args(("A", repo)) + ["--limit", "1"])
        self.assertEqual(code, 0, err)
        self.assertEqual(call.call_count, 1)
        entries = self.read_cache()["entries"]
        self.assertEqual(set(entries), {"A:a.js", "A:b.js"})
        self.assertEqual(entries["A:b.js"], old)
        self.assertEqual(out, f"script_map_summarize: 0 cached, 1 new, 0 failed, 1 deferred, "
                              f"1 pruned -> {self.cache}\n")

    def test_three_consecutive_failures_stop_calls(self):
        repo = self.five_files()
        old = self.entry("0" * 64)
        self.write_cache({"A:f4.js": old})
        fail = script_map_summarize.SummaryError("exit 1: usage limit reached")
        code, out, err, call = self.run_main(self.args(("A", repo)), reply=[fail] * 5)
        self.assertEqual(code, 3)
        self.assertEqual(call.call_count, 3)
        sent = [self.prompt_user(c).split("\n", 1)[0] for c in call.call_args_list]
        self.assertEqual(sent, ["File: f0.js", "File: f1.js", "File: f2.js"])
        self.assertEqual(err.splitlines(), [
            "warning: A:f0.js summary failed: exit 1: usage limit reached",
            "warning: A:f1.js summary failed: exit 1: usage limit reached",
            "warning: A:f2.js summary failed: exit 1: usage limit reached",
            "warning: stopping after 3 consecutive failures; 2 files not attempted",
        ])
        self.assertEqual(self.read_cache()["entries"], {"A:f4.js": old})
        self.assertEqual(out, f"script_map_summarize: 0 cached, 0 new, 5 failed, 0 deferred, "
                              f"0 pruned -> {self.cache}\n")

    def test_success_resets_failure_streak(self):
        repo = self.make_repo("a", {f"f{i}.js": USER_EVENT + f"// {i}\n" for i in range(6)})
        fail = script_map_summarize.SummaryError("exit 1")
        code, out, err, call = self.run_main(self.args(("A", repo)),
                                             reply=[fail, fail, "Ok.", fail, fail, "Ok."])
        self.assertEqual(code, 3)
        self.assertEqual(call.call_count, 6)
        self.assertNotIn("stopping after", err)
        self.assertEqual(set(self.read_cache()["entries"]), {"A:f2.js", "A:f5.js"})
        self.assertEqual(out, f"script_map_summarize: 0 cached, 2 new, 4 failed, 0 deferred, "
                              f"0 pruned -> {self.cache}\n")

    def test_prompt_with_shell_metacharacters_passed_unchanged(self):
        body = USER_EVENT + "// it's \"quoted\" $HOME `id` | rm -rf / ; && >\n"
        repo = self.make_repo("a", {"ue.js": body})
        code, _, err, call = self.run_main(self.args(("A", repo)))
        self.assertEqual(code, 0, err)
        self.assertEqual(call.call_args.args[1],
                         script_map_summarize.SYSTEM_PROMPT + "\n\nFile: ue.js\n"
                         "Script type: UserEventScript\n\n" + body)

    # call_command

    def run_command(self, results, argv=("fake-cmd", "-"), prompt="the prompt", timeout=300):
        with mock.patch("subprocess.run", side_effect=results) as run:
            try:
                return script_map_summarize.call_command(list(argv), prompt, timeout), run
            except script_map_summarize.SummaryError as exc:
                return exc, run

    def done(self, code=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(["fake-cmd"], code, stdout, stderr)

    def test_call_command_pipes_prompt_on_stdin(self):
        prompt = "System.\n\nFile: a.js\n\nconst x = '$(id)';"
        result, run = self.run_command([self.done(stdout="  Summary text.\n\n")],
                                       prompt=prompt, timeout=77)
        self.assertEqual(result, "Summary text.")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args, (["fake-cmd", "-"],))
        self.assertEqual(run.call_args.kwargs, {"input": prompt, "capture_output": True,
                                                "text": True, "timeout": 77})
        self.assertNotIn(prompt, run.call_args.args[0])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.sleep.assert_not_called()

    def test_call_command_blank_stdout_is_empty_response(self):
        for stdout in ("", "  \n\t\n"):
            with self.subTest(stdout=stdout):
                result, run = self.run_command([self.done(stdout=stdout, stderr="progress")])
                self.assertIsInstance(result, script_map_summarize.SummaryError)
                self.assertEqual(result.reason, "empty response")
                self.assertEqual(run.call_count, 1)
        self.sleep.assert_not_called()

    def test_call_command_nonzero_exit_retried_then_raised(self):
        result, run = self.run_command([
            self.done(1, stderr="first\n"),
            self.done(1, stdout="partial", stderr="working...\nerror: usage limit reached\n\n"),
        ])
        self.assertIsInstance(result, script_map_summarize.SummaryError)
        self.assertEqual(str(result), "exit 1: error: usage limit reached")
        self.assertEqual(run.call_count, 2)
        self.sleep.assert_called_once_with(10)

    def test_call_command_long_stderr_cut_to_200(self):
        result, _ = self.run_command([self.done(2, stderr="x"), self.done(2, stderr="y" * 300)])
        self.assertEqual(result.reason, "exit 2: " + "y" * 200)

    def test_call_command_blank_stderr(self):
        result, _ = self.run_command([self.done(1, stderr=""), self.done(5, stderr="  \n")])
        self.assertEqual(result.reason, "exit 5")

    def test_call_command_timeout_retried_then_raised(self):
        timeout = subprocess.TimeoutExpired(["fake-cmd"], 30)
        result, run = self.run_command([timeout, timeout], timeout=30)
        self.assertIsInstance(result, script_map_summarize.SummaryError)
        self.assertEqual(result.reason, "timed out after 30s")
        self.assertEqual(run.call_count, 2)
        self.sleep.assert_called_once_with(10)

    def test_call_command_success_on_retry(self):
        for first in (self.done(1, stderr="busy"), subprocess.TimeoutExpired(["fake-cmd"], 300)):
            with self.subTest(first=type(first).__name__):
                self.sleep.reset_mock()
                result, run = self.run_command([first, self.done(stdout="Fine.\n")])
                self.assertEqual(result, "Fine.")
                self.assertEqual(run.call_count, 2)
                self.sleep.assert_called_once_with(10)


if __name__ == "__main__":
    unittest.main()
