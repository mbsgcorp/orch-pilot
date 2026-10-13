import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import script_map_gitinfo  # noqa: E402

SCRIPT = TOOLS_DIR / "script_map_gitinfo.py"
EMAIL_ONE = "ann.secret@example.invalid"
EMAIL_TWO = "bo.hidden@example.invalid"
DATE_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


class GitInfoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out = self.root / "out" / "gitinfo.json"

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, repo, *args, env=None):
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def make_repo(self, name="a", author="Ann Lee", email=EMAIL_ONE):
        repo = self.root / name
        repo.mkdir(parents=True)
        self.git(repo, "init", "-q")
        self.git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
        self.git(repo, "config", "user.name", author)
        self.git(repo, "config", "user.email", email)
        self.git(repo, "config", "commit.gpgsign", "false")
        return repo

    def commit(self, repo, files, date, message="change", author=None):
        """Write {rel: text} and commit them with a fixed author/committer date."""
        for rel, text in files.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.git(repo, "add", "-A")
        env = dict(os.environ, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        args = ["commit", "-q", "-m", message]
        if author:
            args += ["--author", author]
        self.git(repo, *args, env=env)
        return self.git(repo, "rev-parse", "HEAD").strip()

    def run_main(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = script_map_gitinfo.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def run_ok(self, argv):
        code, out, err = self.run_main(argv)
        self.assertEqual(code, 0, err)
        return out, json.loads(self.out.read_text(encoding="utf-8"))

    def assert_failed(self, argv, message):
        code, out, err = self.run_main(argv)
        self.assertEqual(code, 2)
        self.assertIn(message, err)
        self.assertEqual(out, "")
        self.assertFalse(self.out.exists())
        self.assertFalse(self.out.with_name(self.out.name + ".tmp").exists())

    # AC3 / AC5: history, paths and output shape

    def test_last_commit_wins_and_two_authors(self):
        repo = self.make_repo()
        first = self.commit(repo, {"a.js": "1", "b.js": "1"}, "2026-03-01T10:00:00-07:00")
        second = self.commit(repo, {"a.js": "2"}, "2026-03-02T10:00:00-07:00",
                             author=f"Bo Chan <{EMAIL_TWO}>")
        third = self.commit(repo, {"b.js": "3"}, "2026-03-03T10:00:00-07:00")
        self.commit(repo, {"notes.txt": "x"}, "2026-03-04T10:00:00-07:00")
        _, data = self.run_ok(["--repo", f"A={repo}", "--out", str(self.out)])
        a_last = {"commit": second, "author": "Bo Chan", "date": "2026-03-02T17:00:00Z",
                  "subject": "change", "pr": ""}
        a_first = {"commit": first, "author": "Ann Lee", "date": "2026-03-01T17:00:00Z",
                   "subject": "change", "pr": ""}
        b_last = {"commit": third, "author": "Ann Lee", "date": "2026-03-03T17:00:00Z",
                  "subject": "change", "pr": ""}
        self.assertEqual(data["files"], {
            "A:a.js": dict(a_last, history=[a_last, a_first]),
            "A:b.js": dict(b_last, history=[b_last, dict(a_first)]),
        })
        self.assertNotEqual(first, second)

    def test_spaces_non_ascii_and_nested_paths(self):
        repo = self.make_repo(author="Zoë Ünal")
        sha = self.commit(repo, {"My Scripts/Ordér hold.js": "1",
                                 "deep/er/nest/lib.js": "1",
                                 "deep/readme.md": "1",
                                 "deep/data.json": "1",
                                 "deep/x.jsx": "1"},
                          "2026-03-01T10:00:00+05:30")
        _, data = self.run_ok(["--repo", f"A={repo}", "--out", str(self.out)])
        expected = {"commit": sha, "author": "Zoë Ünal", "date": "2026-03-01T04:30:00Z",
                    "subject": "change", "pr": ""}
        expected = dict(expected, history=[dict(expected)])
        self.assertEqual(data["files"], {
            "A:My Scripts/Ordér hold.js": expected,
            "A:deep/er/nest/lib.js": expected,
        })

    def test_output_shape_utc_and_no_email(self):
        repo = self.make_repo()
        sha = self.commit(repo, {"ue.js": "1"}, "2026-12-31T22:30:15-07:00")
        self.commit(repo, {"Sub/b.js": "1"}, "2026-01-01T00:00:00+00:00",
                    author=f"Bo Chan <{EMAIL_TWO}>")
        _, data = self.run_ok(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(sorted(data), ["files", "generated_at", "repos", "version"])
        self.assertEqual(data["version"], 1)
        self.assertRegex(data["generated_at"], DATE_RE)
        expected = {"commit": sha, "author": "Ann Lee", "date": "2027-01-01T05:30:15Z",
                    "subject": "change", "pr": ""}
        self.assertEqual(data["files"]["A:ue.js"], dict(expected, history=[expected]))
        self.assertRegex(data["files"]["A:ue.js"]["commit"], r"^[0-9a-f]{40}$")
        text = self.out.read_text(encoding="utf-8")
        self.assertNotIn(EMAIL_ONE, text)
        self.assertNotIn(EMAIL_TWO, text)
        self.assertNotIn("@", text)

    def test_empty_and_merge_commits_do_not_break_parsing(self):
        repo = self.make_repo()
        self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        env = dict(os.environ, GIT_AUTHOR_DATE="2026-03-02T10:00:00-07:00",
                   GIT_COMMITTER_DATE="2026-03-02T10:00:00-07:00")
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "empty", env=env)
        self.git(repo, "checkout", "-q", "-b", "side")
        side = self.commit(repo, {"b.js": "1"}, "2026-03-03T10:00:00-07:00")
        self.git(repo, "checkout", "-q", "main")
        main_sha = self.commit(repo, {"a.js": "2"}, "2026-03-04T10:00:00-07:00")
        env = dict(os.environ, GIT_AUTHOR_DATE="2026-03-05T10:00:00-07:00",
                   GIT_COMMITTER_DATE="2026-03-05T10:00:00-07:00")
        self.git(repo, "merge", "-q", "--no-ff", "-m", "merge", "side", env=env)
        _, data = self.run_ok(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(data["files"]["A:a.js"]["commit"], main_sha)
        self.assertEqual(data["files"]["A:b.js"]["commit"], side)

    def test_two_repos_keyed_by_label_and_stdout_line(self):
        a = self.make_repo("a")
        b = self.make_repo("b", author="Bo Chan", email=EMAIL_TWO)
        sha_a = self.commit(a, {"x.js": "1", "y.js": "1"}, "2026-03-01T10:00:00-07:00")
        sha_b = self.commit(b, {"x.js": "1"}, "2026-03-01T10:00:00-07:00")
        out, data = self.run_ok(["--repo", f"A={a}", "--repo", f"B={b}",
                                 "--out", str(self.out)])
        self.assertEqual(out, f"script_map_gitinfo: 3 files, 2 repos -> {self.out}\n")
        self.assertEqual(data["files"]["A:x.js"]["commit"], sha_a)
        self.assertEqual(data["files"]["B:x.js"]["commit"], sha_b)
        self.assertEqual(data["files"]["B:x.js"]["author"], "Bo Chan")

    def test_git_command_shape(self):
        repo = self.make_repo()
        self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        real = subprocess.run
        calls = []

        def spy(argv, **kwargs):
            calls.append((argv, kwargs))
            return real(argv, **kwargs)

        with mock.patch.object(script_map_gitinfo.subprocess, "run", spy):
            self.run_ok(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(len(calls), 2)
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["git", "-C", str(repo), "-c", "core.quotepath=off", "log",
                                "--no-renames", "-z", "--name-only",
                                "--format=\x1e%H\x1f%an\x1f%cI\x1f%s"])
        self.assertEqual(kwargs["timeout"], 120)
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn("shell", kwargs)
        remote_argv, remote_kwargs = calls[1]
        self.assertEqual(remote_argv, ["git", "-C", str(repo), "config", "--get",
                                       "remote.origin.url"])
        self.assertEqual(remote_kwargs["timeout"], 120)
        self.assertEqual(remote_kwargs["env"], kwargs["env"])
        self.assertNotIn("shell", remote_kwargs)

    # Commit subject, PR number, history and GitHub remote

    def test_history_of_five_with_pr(self):
        repo = self.make_repo()
        shas = []
        for day in range(1, 7):
            message = "Fix deposit calc (#12)" if day == 6 else f"Step {day}"
            shas.append(self.commit(repo, {"dep.js": str(day)},
                                    f"2026-03-0{day}T10:00:00-07:00", message=message))
        _, data = self.run_ok(["--repo", f"A={repo}", "--out", str(self.out)])
        entry = data["files"]["A:dep.js"]
        self.assertEqual(entry["commit"], shas[5])
        self.assertEqual(entry["author"], "Ann Lee")
        self.assertEqual(entry["date"], "2026-03-06T17:00:00Z")
        self.assertEqual(entry["subject"], "Fix deposit calc (#12)")
        self.assertEqual(entry["pr"], "12")
        history = entry["history"]
        self.assertEqual(len(history), 5)
        self.assertEqual([item["commit"] for item in history], shas[5:0:-1])
        self.assertEqual(history[0], {key: entry[key] for key in
                                      ("commit", "author", "date", "subject", "pr")})
        self.assertEqual(history[1], {"commit": shas[4], "author": "Ann Lee",
                                      "date": "2026-03-05T17:00:00Z", "subject": "Step 5",
                                      "pr": ""})
        for item in history:
            self.assertEqual(sorted(item), ["author", "commit", "date", "pr", "subject"])

    def test_subject_without_trailing_pr(self):
        repo = self.make_repo()
        self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00",
                    message="Fix (#12) deposit calc")
        _, data = self.run_ok(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(data["files"]["A:a.js"]["subject"], "Fix (#12) deposit calc")
        self.assertEqual(data["files"]["A:a.js"]["pr"], "")

    def test_long_subject_cut_to_200(self):
        repo = self.make_repo()
        subject = "S" * 250
        self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00", message=subject)
        _, data = self.run_ok(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(data["files"]["A:a.js"]["subject"], "S" * 200)
        self.assertEqual(data["files"]["A:a.js"]["history"][0]["subject"], "S" * 200)

    def test_github_slug(self):
        good = ["https://github.com/o/r", "https://github.com/o/r/",
                "https://github.com/o/r.git", "git@github.com:o/r.git",
                "ssh://git@github.com/o/r",
                "https://x-access-token:SECRET123@github.com/o/r.git",
                "https://GitHub.COM/o/r"]
        for url in good:
            with self.subTest(url=url):
                self.assertEqual(script_map_gitinfo.github_slug(url), "o/r")
        bad = ["https://gitlab.com/o/r", "http://github.com/o/r",
               "https://github.com/o/r/extra", "https://github.com/../r",
               "https://github.com/o/..", "https://github.com/o", "", None,
               "https://evil.example/github.com/o/r", "git@github.com/o/r"]
        for url in bad:
            with self.subTest(url=url):
                self.assertEqual(script_map_gitinfo.github_slug(url), "")
        self.assertEqual(script_map_gitinfo.github_slug(
            "https://github.com/mbsgcorp/Demo-Repo.git"), "mbsgcorp/Demo-Repo")

    def test_token_remote_gives_slug_and_never_leaks(self):
        repo = self.make_repo()
        self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        self.git(repo, "remote", "add", "origin",
                 "https://x-access-token:SECRET123@github.com/mbsgcorp/Demo-Repo.git")
        code, out, err = self.run_main(["--repo", f"L={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0, err)
        text = self.out.read_text(encoding="utf-8")
        self.assertEqual(json.loads(text)["repos"], {"L": "mbsgcorp/Demo-Repo"})
        for stream in (text, out, err):
            self.assertNotIn("SECRET123", stream)
            self.assertNotIn("x-access-token", stream)
        self.assertEqual(err, "")
        self.assertEqual(out, f"script_map_gitinfo: 1 files, 1 repos -> {self.out}\n")

    def test_no_remote_warns_and_exits_zero(self):
        repo = self.make_repo()
        self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        code, out, err = self.run_main(["--repo", f"L={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(self.out.read_text(encoding="utf-8"))["repos"], {"L": ""})
        self.assertEqual(err, "warning: L has no GitHub remote; links omitted\n")
        self.assertEqual(out, f"script_map_gitinfo: 1 files, 1 repos -> {self.out}\n")

    def test_non_github_remote_warns(self):
        repo = self.make_repo()
        self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        self.git(repo, "remote", "add", "origin", "https://tok:SECRET9@gitlab.com/o/r.git")
        code, _, err = self.run_main(["--repo", f"L={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "warning: L has no GitHub remote; links omitted\n")
        self.assertNotIn("SECRET9", self.out.read_text(encoding="utf-8"))

    def test_remote_lookup_failure_is_not_an_error(self):
        repo = self.make_repo()
        self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        real = subprocess.run

        def remote_times_out(argv, **kwargs):
            if "config" in argv:
                raise subprocess.TimeoutExpired("git", 120)
            return real(argv, **kwargs)

        with mock.patch.object(script_map_gitinfo.subprocess, "run", remote_times_out):
            code, _, err = self.run_main(["--repo", f"L={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "warning: L has no GitHub remote; links omitted\n")
        self.assertEqual(json.loads(self.out.read_text(encoding="utf-8"))["repos"], {"L": ""})

    # AC1 / AC2: arguments, branch and shallow guards

    def test_help(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("--repo", "--out", "--branch"):
            self.assertIn(flag, result.stdout)

    def test_argument_errors(self):
        repo = self.make_repo()
        self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        afile = self.root / "file.txt"
        afile.write_text("x", encoding="utf-8")
        cases = [
            (["--repo", f"A={repo}", "--branch", "bad branch"],
             "error: --branch must be a branch name"),
            (["--repo", "nolabel"], "error: --repo 'nolabel' must be LABEL=PATH"),
            (["--repo", f"A={repo}", "--repo", f"A={repo}"], "error: duplicate repo label 'A'"),
            (["--repo", f"A={self.root / 'none'}"],
             f"error: A path {str(self.root / 'none')!r} does not exist"),
            (["--repo", f"A={afile}"], f"error: A path {str(afile)!r} is not a directory"),
        ]
        for argv, message in cases:
            with self.subTest(message=message):
                self.assert_failed(argv + ["--out", str(self.out)], message)

    def test_missing_out_is_usage_error(self):
        repo = self.make_repo()
        with contextlib.redirect_stderr(io.StringIO()):
            code = script_map_gitinfo.main(["--repo", f"A={repo}"])
        self.assertEqual(code, 2)

    def test_wrong_branch(self):
        repo = self.make_repo()
        self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        self.git(repo, "checkout", "-q", "-b", "feature")
        self.assert_failed(["--repo", f"A={repo}", "--out", str(self.out)],
                           "error: A is on feature, not main")

    def test_custom_branch(self):
        repo = self.make_repo()
        self.git(repo, "symbolic-ref", "HEAD", "refs/heads/sandbox")
        sha = self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        _, data = self.run_ok(["--repo", f"A={repo}", "--out", str(self.out),
                               "--branch", "sandbox"])
        self.assertEqual(data["files"]["A:a.js"]["commit"], sha)
        self.out.unlink()
        self.assert_failed(["--repo", f"A={repo}", "--out", str(self.out)],
                           "error: A is on sandbox, not main")

    def test_branch_not_verified_without_git_dir(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.assert_failed(["--repo", f"A={plain}", "--out", str(self.out)],
                           "error: A branch not verified")

    def test_shallow_clone(self):
        repo = self.make_repo()
        self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        (repo / ".git" / "shallow").write_text("0" * 40 + "\n", encoding="utf-8")
        self.assert_failed(["--repo", f"A={repo}", "--out", str(self.out)],
                           "error: A is a shallow clone; full history is needed")

    def test_validates_every_repo_before_running_git(self):
        good = self.make_repo("good")
        self.commit(good, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        bad = self.make_repo("bad")
        self.commit(bad, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        (bad / ".git" / "shallow").write_text("", encoding="utf-8")
        with mock.patch.object(script_map_gitinfo.subprocess, "run") as run:
            self.assert_failed(["--repo", f"G={good}", "--repo", f"B={bad}",
                                "--out", str(self.out)],
                               "error: B is a shallow clone; full history is needed")
        run.assert_not_called()

    # AC4: git failures

    def fake_repo(self, name="fake"):
        repo = self.root / name
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        return repo

    def test_git_failure_not_a_real_repo(self):
        repo = self.fake_repo()
        code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        line = err.strip()
        self.assertTrue(line.startswith("error: A git log failed: "), err)
        self.assertLessEqual(len(line) - len("error: A git log failed: "), 200)
        self.assertFalse(self.out.exists())

    def test_git_failure_in_later_repo_writes_nothing(self):
        good = self.make_repo("good")
        self.commit(good, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        empty = self.make_repo("empty")
        code, _, err = self.run_main(["--repo", f"G={good}", "--repo", f"E={empty}",
                                      "--out", str(self.out)])
        self.assertEqual(code, 2)
        self.assertIn("error: E git log failed: ", err)
        self.assertFalse(self.out.exists())

    def test_git_stderr_cut_to_200(self):
        repo = self.make_repo()
        failed = subprocess.CompletedProcess([], 128, "", "first\n" + "x" * 300 + "\n\n")
        with mock.patch.object(script_map_gitinfo.subprocess, "run", return_value=failed):
            self.assert_failed(["--repo", f"A={repo}", "--out", str(self.out)],
                               "error: A git log failed: " + "x" * 200 + "\n")

    def test_git_timeout(self):
        repo = self.make_repo()
        with mock.patch.object(script_map_gitinfo.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("git", 120)):
            self.assert_failed(["--repo", f"A={repo}", "--out", str(self.out)],
                               "error: A git log failed: timed out")

    def test_git_missing(self):
        repo = self.make_repo()
        self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        result = subprocess.run([sys.executable, str(SCRIPT), "--repo", f"A={repo}",
                                 "--out", str(self.out)],
                                capture_output=True, text=True, env=dict(os.environ, PATH=""))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), "error: git not found")
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.out.exists())

    # AC5: atomic write

    def test_write_failure_leaves_nothing(self):
        repo = self.make_repo()
        self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        self.out.parent.mkdir(parents=True)
        self.out.write_text("old", encoding="utf-8")
        tmp = self.out.with_name(self.out.name + ".tmp")
        with mock.patch.object(script_map_gitinfo.os, "replace",
                               side_effect=OSError("disk full")):
            code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 2)
        self.assertIn(f"error: cannot write {self.out}: disk full", err)
        self.assertEqual(out, "")
        self.assertFalse(tmp.exists())
        self.assertEqual(self.out.read_text(encoding="utf-8"), "old")

    def test_write_failure_new_file_leaves_nothing(self):
        repo = self.make_repo()
        self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        with mock.patch.object(script_map_gitinfo.os, "replace",
                               side_effect=OSError("disk full")):
            code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 2)
        self.assertIn("error: cannot write", err)
        self.assertFalse(self.out.exists())
        self.assertFalse(self.out.with_name(self.out.name + ".tmp").exists())

    def test_successful_write_leaves_no_tmp(self):
        repo = self.make_repo()
        self.commit(repo, {"a.js": "1"}, "2026-03-01T10:00:00-07:00")
        self.run_ok(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertFalse(self.out.with_name(self.out.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
