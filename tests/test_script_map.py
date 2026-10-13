import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS_DIR))

import script_map  # noqa: E402

SCRIPT = TOOLS_DIR / "script_map.py"
TEMPLATE = TOOLS_DIR / "script_map_template.html"
DATA_RE = re.compile(
    r'<script type="application/json" id="script-map-data">(.*?)</script>', re.DOTALL)
SCRIPT_TAG_RE = re.compile(r"<script(\s[^>]*)?>(.*?)</script>", re.DOTALL)

USER_EVENT = """/**
 * @NApiVersion 2.1
 * @NScriptType UserEventScript
 * DEPLOY ON: customrecord2326 (Sales Order Hold)
 */
define(['N/record', 'N/search', './SO Hold Creation'], (record, search, hold) => {
    const beforeLoad = (ctx) => {
        try {
            record.load({ type: record.Type.SALES_ORDER, id: ctx.newRecord.id });
        } catch (e) {
            log.error(e);
        }
    };
    return { beforeLoad };
});
"""

LIBRARY = """/**
 * @NApiVersion 2.1
 */
define(['N/log'], (log) => {
    function hold() { try { return 1; } catch (e) { return 0; } }
    return { hold: hold };
});
"""

V1_SCRIPT = """function afterSubmit(type) {
    var rec = nlapiLoadRecord('salesorder', nlapiGetRecordId());
}
"""

CLIENT_SCRIPT = """/**
 * @NApiVersion 2.1
 * @NScriptType ClientScript
 */
define(['N/record'], (record) => {
    const pageInit = (ctx) => {
        record.create({ type: 'customer' });
    };
    return { pageInit };
});
"""

# Three scripts across two repos for the search tests. Req.Dep.js and hold.js both
# deploy on customrecord2326; req_other.js has no DEPLOY ON: line.
SEARCH_REPOS = {
    "RTMH": {"Req.Dep.js": USER_EVENT, "req_other.js": CLIENT_SCRIPT},
    "WCS": {"hold.js": USER_EVENT.replace("SALES_ORDER", "INVOICE")},
}

DARK_THEME = {
    "--bg": "#15171b", "--panel": "#1e2126", "--text": "#e6e8eb", "--muted": "#9aa0a9",
    "--border": "#363a42", "--accent": "#6ea8ec", "--edge": "#6b717b", "--record": "#c6cbd3",
    "--unresolved": "#5c6068", "--high-bg": "#3d1f1e", "--high-border": "#f0675f",
    "--medium-bg": "#3a3018", "--medium-border": "#e3ac3b", "--low-bg": "#1c3324",
    "--low-border": "#5cc27f", "--badge-bg": "#2a3242", "--accent-contrast": "#0f1115",
}

LIGHT_THEME = {
    "--bg": "#f7f7f8", "--panel": "#ffffff", "--text": "#1d1f23", "--muted": "#61666f",
    "--border": "#d8dbe0", "--accent": "#2b6cb0", "--edge": "#8a909a", "--record": "#3d4451",
    "--unresolved": "#a0a4ab", "--high-bg": "#fde2e1", "--high-border": "#d93a33",
    "--medium-bg": "#fdf1d6", "--medium-border": "#c98a0c", "--low-bg": "#e1f3e6",
    "--low-border": "#2f8a4e", "--badge-bg": "#e6ecf7", "--accent-contrast": "#ffffff",
}

STORAGE_PRELUDE = """
var STORE = %s;
var window = { localStorage: {
    getItem: function (k) { return k in STORE ? STORE[k] : null; },
    setItem: function (k, v) { STORE[k] = String(v); }
} };
document.documentElement = new Elem('html');
"""

THROWING_STORAGE_PRELUDE = """
var window = { localStorage: {
    getItem: function () { throw new Error('denied'); },
    setItem: function () { throw new Error('denied'); }
} };
document.documentElement = new Elem('html');
"""

THEME_TAIL = """
var states = [themeState()];
click(themeButton());
states.push(themeState());
click(themeButton());
states.push(themeState());
console.log(JSON.stringify(states));
"""

# Shared-filter fixture on SEARCH_REPOS: hold.js (WCS) becomes a high-risk 1.0 script, so
# RTMH has Req.Dep.js (UE 2.1 low) and req_other.js (CS 2.1 medium).
FILTER_PRELUDE = r"""
enableQuerySelector();
enableNav();
editData(function (d) {
    d.nodes.forEach(function (n) {
        if (n.kind === 'script' && n.name === 'hold.js') { n.version = '1.0'; n.risk.level = 'high'; }
    });
});
function chipButtons() {
    return byIdMap['repo-chips'].children.filter(function (c) { return c.tagName === 'button'; });
}
function chipByRepo(repo) {
    return chipButtons().filter(function (c) { return c.textContent.split(' · ')[0] === repo; })[0];
}
function chipState() {
    return chipButtons().map(function (c) {
        return [c.textContent.split(' · ')[0], c.type, c.className, c.getAttribute('aria-pressed')];
    });
}
function tileBy(id, label) {
    return byIdMap[id].children.filter(function (c) { return c.children[0].textContent === label; })[0];
}
function tileState(id) {
    return byIdMap[id].children.map(function (c) {
        return [c.tagName, c.type, c.className, c.getAttribute('aria-pressed'),
                c.children[0].textContent, c.children[1].children[0].textContent];
    });
}
function filters() {
    return { search: byIdMap['script-search'].value, repo: byIdMap['i-repo'].value,
             type: byIdMap['i-type'].value, version: byIdMap['i-version'].value,
             risk: byIdMap['i-risk'].value };
}
function change(id, value) {
    byIdMap[id].value = value;
    fire(byIdMap[id], 'change');
}
function riskCard(name) {
    return byIdMap['r-grid'].children.filter(function (c) {
        return c.className.indexOf('card') === 0 && c.children[0].textContent === name;
    })[0];
}
function gridState() {
    return byIdMap['r-grid'].children.map(function (c) {
        if (c.className === 'r-detail') {
            var d = c.children[0];
            return ['r-detail', c.children.length, d.tagName, !!d.open, entryName(d)];
        }
        return [c.className.split(' ')[0], c.children[0].textContent, c.getAttribute('aria-expanded')];
    });
}
function views() {
    return ['view-inventory', 'view-records', 'view-risk'].filter(function (id) {
        return !byIdMap[id].hidden;
    });
}
function view() {
    var s = snapshot();
    return { filters: filters(), list: s.list.sort(), risk: s.risk.sort(), areas: s.areas,
             missing: s.missing, chips: chipState(), shown: views() };
}
"""

HASH_PRELUDE = r"""
var HASH_WRITES = [];
var HASH_LISTENERS = {};
var window = {
    location: { hash: %s },
    history: { replaceState: function (s, t, h) { HASH_WRITES.push(h); window.location.hash = h; } },
    addEventListener: function (type, fn) {
        (HASH_LISTENERS[type] = HASH_LISTENERS[type] || []).push(fn);
    }
};
"""


class ScriptMapTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out = self.root / "out" / "map.html"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, text=""):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_bytes(text.encode("utf-8"))
        return path

    def make_repo(self, name, head="ref: refs/heads/main\n"):
        repo = self.root / name
        repo.mkdir(parents=True, exist_ok=True)
        if head is not None:
            (repo / ".git").mkdir(exist_ok=True)
            (repo / ".git" / "HEAD").write_text(head, encoding="utf-8")
        return repo

    def run_main(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = script_map.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def collect_one(self, repo, label="A"):
        return script_map.collect([(label, repo)])

    def scripts(self, data):
        return {n["path"]: n for n in data["nodes"] if n["kind"] == "script"}

    def edges(self, data):
        return {(e["source"], e["target"], e["kind"]) for e in data["edges"]}

    def read_data(self, path=None):
        text = (path or self.out).read_text(encoding="utf-8")
        match = DATA_RE.search(text)
        self.assertIsNotNone(match)
        return match.group(1), json.loads(match.group(1))

    # AC1 / AC2: CLI and branch guard

    def test_help_exits_zero(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--repo", result.stdout)

    def test_help_mentions_refresh_url(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--refresh-url", result.stdout)

    def test_refresh_url_set(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                        "--refresh-url", "/api/rebuild"])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("script_map: "))
        _, data = self.read_data()
        self.assertEqual(data["refresh_url"], "/api/rebuild")

    def test_refresh_url_default_empty(self):
        repo = self.make_repo("a")
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0, err)
        _, data = self.read_data()
        self.assertEqual(data["refresh_url"], "")

    def test_refresh_url_invalid(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        for value in ("api/rebuild", "https://example.test/api/rebuild", "/api/re build"):
            with self.subTest(value=value):
                code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                                "--refresh-url", value])
                self.assertEqual(code, 2)
                self.assertEqual(
                    err, "error: --refresh-url must be a site-relative path starting with /\n")
                self.assertEqual(out, "")
                self.assertFalse(self.out.exists())

    def test_main_branch_passes(self):
        repo = self.make_repo("a")
        self.write("a/UserEvent/ue.js", USER_EVENT)
        code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertTrue(self.out.exists())
        _, data = self.read_data()
        self.assertEqual(data["repos"], [{"label": "A", "branch": "main", "script_count": 1}])

    def test_sandbox_branch_fails(self):
        repo = self.make_repo("a", "ref: refs/heads/sandbox\n")
        code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "error: A is on sandbox, not main")
        self.assertEqual(out, "")
        self.assertFalse(self.out.exists())

    def test_detached_head_fails(self):
        repo = self.make_repo("a", "0123456789abcdef0123456789abcdef01234567\n")
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "error: A is on detached HEAD, not main")
        self.assertFalse(self.out.exists())

    def test_second_repo_on_wrong_branch_writes_nothing(self):
        good = self.make_repo("a")
        bad = self.make_repo("b", "ref: refs/heads/feature/x\n")
        code, _, err = self.run_main(["--repo", f"A={good}", "--repo", f"B={bad}",
                                      "--out", str(self.out)])
        self.assertEqual(code, 2)
        self.assertIn("error: B is on feature/x, not main", err)
        self.assertFalse(self.out.exists())

    def test_no_git_warns_and_marks_unverified(self):
        repo = self.make_repo("a", head=None)
        self.write("a/ue.js", USER_EVENT)
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0)
        self.assertEqual(err.strip(), "warning: A branch not verified")
        _, data = self.read_data()
        self.assertEqual(data["repos"][0]["branch"], "branch not verified")

    def test_git_file_warns(self):
        repo = self.make_repo("a", head=None)
        self.write("a/.git", "gitdir: /elsewhere\n")
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0)
        self.assertIn("warning: A branch not verified", err)

    def test_unreadable_head_is_error(self):
        repo = self.make_repo("a", head=None)
        (repo / ".git").mkdir()
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 2)
        self.assertIn("A", err)
        self.assertIn("HEAD", err)
        self.assertFalse(self.out.exists())

    def test_collect_raises_on_unreadable_head(self):
        repo = self.make_repo("a", head=None)
        (repo / ".git").mkdir()
        with self.assertRaises(OSError):
            self.collect_one(repo)

    def test_head_unreadable_after_guard_is_error(self):
        repo = self.make_repo("a")
        real = script_map.branch_status
        calls = []

        def flaky(path, expected="main"):
            calls.append(path)
            if len(calls) > 1:
                (Path(path) / ".git" / "HEAD").unlink()
            return real(path, expected)

        with mock.patch.object(script_map, "branch_status", flaky):
            code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith(f"error: A cannot read {repo / '.git' / 'HEAD'}: "), err)
        self.assertFalse(self.out.exists())

    # Branch, environment and toggle options

    def test_branch_status_expected(self):
        sandbox = self.make_repo("s", "ref: refs/heads/sandbox\n")
        main = self.make_repo("m")
        release = self.make_repo("r", "ref: refs/heads/release/x\n")
        plain = self.make_repo("p", head=None)
        self.assertEqual(script_map.branch_status(sandbox, expected="sandbox"), "sandbox")
        self.assertEqual(script_map.branch_status(main, expected="sandbox"), "main")
        self.assertEqual(script_map.branch_status(sandbox), "sandbox")
        self.assertEqual(script_map.branch_status(main), "main")
        self.assertEqual(script_map.branch_status(release, "release/x"), "release/x")
        self.assertIsNone(script_map.branch_status(plain, expected="sandbox"))

    def test_branch_sandbox_end_to_end(self):
        repo = self.make_repo("a", "ref: refs/heads/sandbox\n")
        self.write("a/UserEvent/ue.js", USER_EVENT)
        code, out, err = self.run_main(["--repo", f"RTMH={repo}", "--out", str(self.out),
                                        "--branch", "sandbox"])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertTrue(out.startswith("script_map: 1 scripts, "))
        _, data = self.read_data()
        self.assertEqual(data["repos"],
                         [{"label": "RTMH", "branch": "sandbox", "script_count": 1}])
        self.assertEqual(data["branch"], "sandbox")

    def test_branch_sandbox_on_main_head_fails(self):
        repo = self.make_repo("a")
        code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                        "--branch", "sandbox"])
        self.assertEqual(code, 2)
        self.assertEqual(err.strip(), "error: A is on main, not sandbox")
        self.assertEqual(out, "")
        self.assertFalse(self.out.exists())

    def test_branch_with_slash(self):
        repo = self.make_repo("a", "ref: refs/heads/release/x\n")
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                      "--branch", "release/x"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read_data()[1]["repos"][0]["branch"], "release/x")

    def test_invalid_branch(self):
        repo = self.make_repo("a")
        for value in ("", "bad branch", "a;b", "x$y"):
            with self.subTest(value=value):
                code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                                "--branch", value])
                self.assertEqual(code, 2)
                self.assertEqual(err, "error: --branch must be a branch name\n")
                self.assertEqual(out, "")
                self.assertFalse(self.out.exists())

    def test_environment_fields_default(self):
        repo = self.make_repo("a")
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0, err)
        _, data = self.read_data()
        self.assertEqual(data["environment"], "Production")
        self.assertEqual(data["switch_url"], "")
        self.assertEqual(data["switch_label"], "")
        self.assertEqual(data["missing_repos"], [])
        self.assertEqual(data["branch"], "main")
        self.assertEqual(data["repos"][0]["branch"], "main")

    def test_environment_fields_set(self):
        repo = self.make_repo("a", "ref: refs/heads/sandbox\n")
        code, out, err = self.run_main([
            "--repo", f"A={repo}", "--out", str(self.out), "--branch", "sandbox",
            "--environment", "Sandbox", "--switch-url", "/", "--switch-label", "Production",
            "--missing-repo", "WCS", "--missing-repo", "ANS Co"])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, f"script_map: 0 scripts, 0 records, 0 edges, 0 skipped -> "
                              f"{self.out}\n")
        _, data = self.read_data()
        self.assertEqual(data["environment"], "Sandbox")
        self.assertEqual(data["switch_url"], "/")
        self.assertEqual(data["switch_label"], "Production")
        self.assertEqual(data["missing_repos"], ["WCS", "ANS Co"])
        self.assertEqual(data["branch"], "sandbox")

    def test_empty_environment_rejected(self):
        repo = self.make_repo("a")
        for value in ("", "   "):
            with self.subTest(value=value):
                code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                                "--environment", value])
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("error: --environment "), err)
                self.assertEqual(out, "")
                self.assertFalse(self.out.exists())

    def test_switch_url_requires_label(self):
        repo = self.make_repo("a")
        for extra in ([], ["--switch-label", ""]):
            with self.subTest(extra=extra):
                code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                                "--switch-url", "/sandbox.html"] + extra)
                self.assertEqual(code, 2)
                self.assertEqual(err, "error: --switch-label is required with --switch-url\n")
                self.assertEqual(out, "")
                self.assertFalse(self.out.exists())

    def test_switch_url_invalid(self):
        repo = self.make_repo("a")
        for value in ("sandbox.html", "https://example.test/sandbox.html", "/sand box.html"):
            with self.subTest(value=value):
                code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                                "--switch-url", value, "--switch-label", "X"])
                self.assertEqual(code, 2)
                self.assertEqual(
                    err, "error: --switch-url must be a site-relative path starting with /\n")
                self.assertEqual(out, "")
                self.assertFalse(self.out.exists())

    def test_help_mentions_environment_options(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("--branch", "--environment", "--switch-url", "--switch-label",
                     "--missing-repo"):
            self.assertIn(flag, result.stdout)

    # AC3: input errors

    def assertInputError(self, argv):
        code, out, err = self.run_main(list(argv) + ["--out", str(self.out)])
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())
        self.assertEqual(out, "")
        self.assertFalse(self.out.exists())
        return err

    def test_missing_path(self):
        err = self.assertInputError(["--repo", f"A={self.root / 'nope'}"])
        self.assertIn("does not exist", err)

    def test_path_is_file(self):
        path = self.write("file.txt", "x")
        err = self.assertInputError(["--repo", f"A={path}"])
        self.assertIn("not a directory", err)

    def test_missing_equals(self):
        err = self.assertInputError(["--repo", str(self.make_repo("a"))])
        self.assertIn("LABEL=PATH", err)

    def test_empty_label(self):
        err = self.assertInputError(["--repo", f"={self.make_repo('a')}"])
        self.assertIn("empty label", err)

    def test_duplicate_label(self):
        a = self.make_repo("a")
        b = self.make_repo("b")
        err = self.assertInputError(["--repo", f"A={a}", "--repo", f"A={b}"])
        self.assertIn("duplicate", err)

    def test_parse_repo_arg(self):
        self.assertEqual(script_map.parse_repo_arg("My Client=/x=y"),
                         ("My Client", Path("/x=y")))
        with self.assertRaises(ValueError):
            script_map.parse_repo_arg("nolabel")

    def test_missing_required_args(self):
        code, _, err = self.run_main([])
        self.assertEqual(code, 2)
        self.assertIn("--repo", err)

    # AC4: discovery

    def test_exclusions(self):
        repo = self.make_repo("a")
        self.write("a/UserEvent/keep.js", USER_EVENT)
        for rel in ("sandbox/s.js", "backups/b.js", "Backup/b2.js", "Tests/t.js",
                    "test/t2.js", "node_modules/pkg/n.js", "temp-handoffs/h.js",
                    "UserEvent/x.min.js", "UserEvent/notes.txt"):
            self.write(f"a/{rel}", USER_EVENT)
        self.write("a/.git/hooks/g.js", USER_EVENT)
        data = self.collect_one(repo)
        self.assertEqual(list(self.scripts(data)), ["UserEvent/keep.js"])
        self.assertEqual(data["skipped"], [])

    def test_too_large_skipped(self):
        repo = self.make_repo("a")
        self.write("a/big.js", "// @NApiVersion 2.1\n" + "x" * 2_000_001)
        data = self.collect_one(repo)
        self.assertEqual(data["skipped"], [{"repo": "A", "path": "big.js", "reason": "too large"}])

    def test_unreadable_directory_is_skipped(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can list unreadable directories")
        repo = self.make_repo("a")
        self.write("a/Locked/x.js", USER_EVENT)
        locked = repo / "Locked"
        locked.chmod(0)
        try:
            try:
                os.listdir(locked)
            except PermissionError:
                pass
            else:
                self.skipTest("chmod 0 does not prevent listing here")
            data = self.collect_one(repo)
        finally:
            locked.chmod(0o755)
        entries = [s for s in data["skipped"] if s["path"] == "Locked"]
        self.assertEqual(len(entries), 1, data["skipped"])
        self.assertEqual(entries[0]["repo"], "A")
        self.assertTrue(entries[0]["reason"].startswith("unreadable: "), entries[0])

    def test_crlf_parsed(self):
        repo = self.make_repo("a")
        self.write("a/ClientScript/c.js", USER_EVENT.replace("\n", "\r\n"))
        node = self.scripts(self.collect_one(repo))["ClientScript/c.js"]
        self.assertEqual(node["type"], "UserEventScript")
        self.assertEqual(node["api_version"], "2.1")
        self.assertEqual(node["entry_points"], ["beforeLoad"])
        self.assertTrue(node["has_deploy_line"])

    def test_directory_symlink_not_followed(self):
        repo = self.make_repo("a")
        self.write("outside/o.js", USER_EVENT)
        try:
            (repo / "link").symlink_to(self.root / "outside", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported")
        self.assertEqual(self.scripts(self.collect_one(repo)), {})

    # AC5: classification

    def test_classify(self):
        self.assertEqual(script_map.classify(USER_EVENT), (True, "UserEventScript", "2.1"))
        self.assertEqual(script_map.classify(V1_SCRIPT), (True, "SuiteScript 1.0", "1.0"))
        self.assertEqual(script_map.classify(LIBRARY), (True, "Library", "2.1"))
        self.assertEqual(script_map.classify("/** @NScriptType Suitelet */"),
                         (True, "Suitelet", ""))
        self.assertEqual(script_map.classify("console.log(1);"), (False, "", ""))

    def test_non_suitescript_skipped(self):
        repo = self.make_repo("a")
        self.write("a/plain.js", "console.log('hi');\n")
        self.write("a/v1.js", V1_SCRIPT)
        data = self.collect_one(repo)
        self.assertEqual(data["skipped"],
                         [{"repo": "A", "path": "plain.js", "reason": "not SuiteScript"}])
        self.assertEqual(self.scripts(data)["v1.js"]["type"], "Unknown")

    def test_infer_type_from_path(self):
        infer = script_map.infer_type_from_path
        self.assertEqual(infer("UserEvent/x.js"), "UserEventScript")
        self.assertEqual(infer("User Event Scripts/x.js"), "UserEventScript")
        self.assertEqual(infer("MapReduce/sub/x.js"), "MapReduceScript")
        self.assertEqual(infer("ClientScript/x.js"), "ClientScript")
        self.assertEqual(infer("Suitelet/UserEvent/x.js"), "UserEventScript")
        self.assertIsNone(infer("Shared/x.js"))
        self.assertIsNone(infer("x.js"))
        self.assertIsNone(infer("Shared/UserEvent_x.js"))

    def test_version_bucket(self):
        bucket = script_map.version_bucket
        self.assertEqual(bucket("1.0"), "1.0")
        self.assertEqual(bucket("2.0"), "2.0")
        self.assertEqual(bucket("2.X"), "2.x")
        self.assertEqual(bucket(" 2.1 "), "2.1")
        self.assertEqual(bucket(""), "None")
        self.assertEqual(bucket("3.0"), "Other")

    def test_collect_infers_type_and_version(self):
        repo = self.make_repo("a")
        self.write("a/UserEvent/v1.js", V1_SCRIPT)
        self.write("a/v1.js", V1_SCRIPT)
        self.write("a/UserEvent/ue.js", USER_EVENT)
        scripts = self.scripts(self.collect_one(repo))
        inferred = scripts["UserEvent/v1.js"]
        self.assertEqual(inferred["type"], "UserEventScript")
        self.assertTrue(inferred["type_inferred"])
        self.assertEqual(inferred["version"], "1.0")
        self.assertEqual(inferred["api_version"], "1.0")
        root = scripts["v1.js"]
        self.assertEqual(root["type"], "Unknown")
        self.assertFalse(root["type_inferred"])
        self.assertEqual(root["version"], "1.0")
        declared = scripts["UserEvent/ue.js"]
        self.assertEqual(declared["type"], "UserEventScript")
        self.assertFalse(declared["type_inferred"])
        self.assertEqual(declared["version"], "2.1")

    # AC6: entry points

    def test_entry_points_shorthand(self):
        text = "define([], () => { return {x: 1}; return { beforeLoad, afterSubmit }; });"
        self.assertEqual(script_map.entry_points(text), ["beforeLoad", "afterSubmit"])

    def test_entry_points_key_value(self):
        text = """define([], function () {
    return {
        pageInit: pageInit,
        fieldChanged: function (ctx) { if (a) { b(); } },
        helper: helper,
        saveRecord: save
    };
});"""
        self.assertEqual(script_map.entry_points(text), ["pageInit", "fieldChanged", "saveRecord"])

    def test_entry_points_ignores_nested_keys(self):
        text = "return { helper: { beforeLoad: fn }, list: [a, get], pageInit };"
        self.assertEqual(script_map.entry_points(text), ["pageInit"])

    def test_entry_points_unbalanced_or_missing(self):
        self.assertEqual(script_map.entry_points("return { beforeLoad, { "), [])
        self.assertEqual(script_map.entry_points("function x() {}"), [])
        self.assertEqual(script_map.entry_points("}}} return {"), [])

    # AC7: records

    def test_deploy_targets(self):
        text = (" * DEPLOY ON: customrecord2326 (Sales Order Hold), SalesOrder\n"
                " * deploy on: invoice\n"
                " * DEPLOY ON:\n"
                " * DEPLOY ON: , ()\n")
        self.assertEqual(script_map.deploy_targets(text), [
            ("customrecord2326", "Sales Order Hold"),
            ("salesorder", "salesorder"),
            ("invoice", "invoice"),
        ])

    def test_touched_records(self):
        text = """
record.load({ type: record.Type.SALES_ORDER });
search.create({ type: search.Type.INVENTORY_ITEM });
form.addField({ id: 'custpage_x', type: 'checkbox' });
form.addField({ id: 'custpage_y', type: "text" });
record.create({ type: 'customer' });
record.create({ type: 'Not Valid' });
var t = "customrecord_mbsg_hold";
var u = 'CUSTOMRECORD99';
"""
        self.assertEqual(script_map.touched_records(text),
                         ["salesorder", "inventoryitem", "customer",
                          "customrecord_mbsg_hold", "customrecord99"])

    def test_touched_records_drops_chart_literals(self):
        text = ("new Chart(ctx, { type: 'bar', data: { datasets: [{ type: 'pie' }, "
                "{ type: 'g' }] }, options: { type: 'success' } });")
        self.assertEqual(script_map.touched_records(text), [])

    def test_touched_records_counts_record_calls(self):
        self.assertEqual(script_map.touched_records(
            "record.load({ type: 'salesorder', id: 1 });"), ["salesorder"])
        self.assertEqual(script_map.touched_records(
            "rec.create({ type: 'mycustomthing' });"), ["mycustomthing"])
        self.assertEqual(script_map.touched_records(
            "search.lookupFields({ type: 'vendor', id: x, columns: ['x'] });"), ["vendor"])
        self.assertEqual(script_map.touched_records(
            "search.create({ filters: [['x', 'is', 'T']], columns: [f(1)], "
            "type: 'transaction' });"), ["transaction"])

    def test_touched_records_known_record_outside_call(self):
        self.assertEqual(script_map.touched_records("var opts = { type: 'customer' };"),
                         ["customer"])
        self.assertEqual(script_map.touched_records("var opts = { type: 'widgetthing' };"), [])
        self.assertEqual(script_map.touched_records(
            "var a = { type: 'customrecord_abc' }; var b = { type: 'customtransaction_xyz' };"),
            ["customrecord_abc", "customtransaction_xyz"])
        self.assertEqual(script_map.touched_records(
            "x = { type: 'item' }; y = { type: 'noninventoryitem' };"),
            ["item", "noninventoryitem"])

    def test_touched_records_enum_and_customrecord_unchanged(self):
        self.assertEqual(script_map.touched_records(
            "chart({ a: record.Type.SALES_ORDER }); var t = 'customrecord_zz';"),
            ["salesorder", "customrecord_zz"])

    def test_touched_records_skips_quoted_parens(self):
        self.assertEqual(script_map.touched_records(
            "record.load({ id: ')', type: 'invoice' });"), ["invoice"])
        self.assertEqual(script_map.touched_records(
            "record.load({ id: ')', type: 'invoicex' });"), ["invoicex"])
        self.assertEqual(script_map.touched_records(
            "record.load({ id: \")\", note: `a ) b`, s: 'it\\')s', type: 'widgetthing' });"),
            ["widgetthing"])
        self.assertEqual(script_map.touched_records(
            "record.load({ id: 1 }); var o = { type: 'widgetthing' };"), [])

    def test_touched_records_unclosed_call_limit(self):
        near = "record.load({ " + " " * (100 - 3) + "type: 'widgetthing'"
        self.assertEqual(near.index("type:") - near.index("("), 100)
        self.assertEqual(script_map.touched_records(near), ["widgetthing"])
        far = "record.load({ " + " " * (2100 - 3) + "type: 'widgetthing'"
        self.assertEqual(far.index("type:") - far.index("("), 2100)
        self.assertEqual(script_map.touched_records(far), [])

    def test_collect_drops_chart_literals(self):
        repo = self.make_repo("a")
        self.write("a/Suitelet/sl.js", (
            "/** @NScriptType Suitelet */\n"
            "define(['N/record'], function (record) {\n"
            "  new Chart(ctx, { type: 'bar' }); var c = { type: 'pie' };\n"
            "  var g = { type: 'g' }; showMessage({ type: 'success' });\n"
            "  var w = { type: 'widgetthing' }; var o = { type: 'customer' };\n"
            "  record.load({ type: 'salesorder', id: 1 });\n"
            "  rec.create({ type: 'mycustomthing' });\n"
            "  search.lookupFields({ type: 'vendor', id: x, columns: ['x'] });\n"
            "  var a = { type: 'customrecord_abc', b: 'customtransaction_xyz' };\n"
            "  var b = { type: 'customtransaction_xyz' };\n"
            "  var e = record.Type.INVOICE; var z = 'customrecord_zz';\n"
            "  return { onRequest: function () {} };\n"
            "});\n"))
        data = self.collect_one(repo)
        records = {n["record_id"] for n in data["nodes"] if n["kind"] == "record"}
        self.assertEqual(records, {
            "invoice", "salesorder", "customer", "mycustomthing", "vendor",
            "customrecord_abc", "customtransaction_xyz", "customrecord_zz"})
        touches = {t for s, t, k in self.edges(data) if k == "touches"}
        for dropped in ("bar", "pie", "g", "success", "widgetthing"):
            self.assertNotIn(f"A:{dropped}", touches)
        self.assertIn("A:mycustomthing", touches)

    def test_risk_ignores_dropped_literals(self):
        repo = self.make_repo("a")
        self.write("a/Suitelet/sl.js", (
            "/** @NScriptType Suitelet */\n"
            "try { x(); } catch (e) {}\n"
            "new Chart(ctx, { type: 'bar' }); new Chart(ctx, { type: 'pie' });\n"
            "new Chart(ctx, { type: 'doughnut' });\n"
            "record.load({ type: 'salesorder', id: 1 });\n"))
        node = self.scripts(self.collect_one(repo))["Suitelet/sl.js"]
        self.assertFalse([r for r in node["risk"]["reasons"] if r.startswith("touches")])
        self.assertEqual(node["risk"]["reasons"], [])

    def test_record_edges_and_labels(self):
        repo = self.make_repo("a")
        self.write("a/UserEvent/ue.js", USER_EVENT.replace(
            "record.Type.SALES_ORDER", "'customrecord2326'"))
        data = self.collect_one(repo)
        records = {n["id"]: n for n in data["nodes"] if n["kind"] == "record"}
        self.assertEqual(records["A:customrecord2326"]["label"], "Sales Order Hold")
        edges = self.edges(data)
        self.assertIn(("A:UserEvent/ue.js", "A:customrecord2326", "deploy"), edges)
        self.assertNotIn(("A:UserEvent/ue.js", "A:customrecord2326", "touches"), edges)

    def test_touches_edge(self):
        repo = self.make_repo("a")
        self.write("a/UserEvent/ue.js", USER_EVENT)
        data = self.collect_one(repo)
        self.assertIn(("A:UserEvent/ue.js", "A:salesorder", "touches"), self.edges(data))

    def test_record_category(self):
        cases = {
            "salesorder": "Sales",
            "invoice": "Billing / AR",
            "vendorbill": "Purchasing / AP",
            "itemreceipt": "Purchasing / AP",
            "itemfulfillment": "Sales",
            "inventoryitem": "Items",
            "assemblyitem": "Items",
            "lotnumberedassemblyitem": "Items",
            "item": "Items",
            "workorder": "Inventory / Manufacturing",
            "customer": "Entities",
            "journalentry": "Accounting",
            "customrecord2326": "Custom records",
            "customrecord_mbsg_hold": "Custom records",
            "somethingelse": "Other",
        }
        for record_id, area in cases.items():
            with self.subTest(record_id=record_id):
                self.assertEqual(script_map.record_category(record_id), area)

    def test_collect_record_categories(self):
        repo = self.make_repo("a")
        self.write("a/UserEvent/ue.js", USER_EVENT.replace(
            "record.Type.SALES_ORDER",
            "record.Type.SALES_ORDER }); search.create({ type: search.Type.INVENTORY_ITEM }); "
            "record.create({ type: 'customrecord_mbsg_hold' }); "
            "record.create({ type: 'somethingelse'"))
        data = self.collect_one(repo)
        categories = {n["record_id"]: n["category"]
                      for n in data["nodes"] if n["kind"] == "record"}
        self.assertEqual(categories, {
            "customrecord2326": "Custom records",
            "salesorder": "Sales",
            "inventoryitem": "Items",
            "customrecord_mbsg_hold": "Custom records",
            "somethingelse": "Other",
        })
        self.assertEqual(data["record_categories"], [
            "Sales", "Billing / AR", "Purchasing / AP", "Items",
            "Inventory / Manufacturing", "Entities", "Accounting", "Custom records", "Other",
        ])

    def test_records_keyed_per_repo(self):
        a = self.make_repo("a")
        b = self.make_repo("b")
        self.write("a/UserEvent/ue.js", USER_EVENT)
        self.write("b/UserEvent/ue.js", USER_EVENT)
        data = script_map.collect([("A", a), ("B", b)])
        ids = [n["id"] for n in data["nodes"] if n["kind"] == "record"]
        self.assertIn("A:customrecord2326", ids)
        self.assertIn("B:customrecord2326", ids)
        scripts = [n["id"] for n in data["nodes"] if n["kind"] == "script"]
        self.assertEqual(scripts, ["A:UserEvent/ue.js", "B:UserEvent/ue.js"])

    # AC8: dependencies

    def test_library_resolution_with_spaces_and_unresolved(self):
        repo = self.make_repo("a")
        self.write("a/UserEvent/ue.js", USER_EVENT.replace(
            "'./SO Hold Creation'", "'./SO Hold Creation', '../Shared/missing_lib.js'"))
        self.write("a/Shared/SO Hold Creation.js", LIBRARY)
        self.write("a/Other/so hold creation.js", LIBRARY)
        data = self.collect_one(repo)
        edges = self.edges(data)
        self.assertIn(("A:UserEvent/ue.js", "A:Shared/SO Hold Creation.js", "library"), edges)
        self.assertIn(("A:UserEvent/ue.js", "A:Other/so hold creation.js", "library"), edges)
        target = "A:unresolved:../Shared/missing_lib.js"
        self.assertIn(("A:UserEvent/ue.js", target, "library"), edges)
        unresolved = [n for n in data["nodes"] if n["kind"] == "unresolved"]
        self.assertEqual(unresolved, [{"id": target, "kind": "unresolved", "repo": "A",
                                       "label": "../Shared/missing_lib.js"}])
        self.assertEqual(self.scripts(data)["UserEvent/ue.js"]["modules"],
                         ["N/record", "N/search"])

    def test_client_module_path(self):
        repo = self.make_repo("a")
        self.write("a/Suitelet/sl.js", "/** @NScriptType Suitelet */\n"
                   "form.clientScriptModulePath = 'SuiteScripts/ClientScript/cs_form.js';\n"
                   "form.clientScriptModulePath = './nowhere.js';\n")
        self.write("a/ClientScript/cs_form.js", "/** @NScriptType ClientScript */\n")
        edges = self.edges(self.collect_one(repo))
        self.assertIn(("A:Suitelet/sl.js", "A:ClientScript/cs_form.js", "client"), edges)
        self.assertIn(("A:Suitelet/sl.js", "A:unresolved:./nowhere.js", "client"), edges)

    # AC9: risk

    def test_risk_each_factor(self):
        base = "try { x(); } catch (e) {}\n"
        self.assertEqual(script_map.risk(base, 0),
                         {"score": 0, "level": "low", "reasons": []})
        self.assertEqual(script_map.risk(base + "nlapiLoadRecord();", 0)["reasons"],
                         ["SuiteScript 1.0"])
        self.assertEqual(script_map.risk("x();", 0),
                         {"score": 2, "level": "medium", "reasons": ["no try/catch"]})
        long_text = base + "x();\n" * 1841
        self.assertEqual(script_map.risk(long_text, 0),
                         {"score": 2, "level": "medium", "reasons": ["1,842 lines"]})
        ids = base + "".join(f"var o = {{ id: {n}, internalid: '{n}' }};\n" for n in range(4))
        self.assertEqual(script_map.risk(ids, 0),
                         {"score": 1, "level": "low", "reasons": ["8 hardcoded IDs"]})
        five = base + "id = 1; id = 2; id = 3; id = 4; id = 5;"
        self.assertEqual(script_map.risk(five, 0)["score"], 0)
        self.assertEqual(script_map.risk(base, 4),
                         {"score": 1, "level": "low", "reasons": ["touches 4 records"]})
        self.assertEqual(script_map.risk(base, 2)["score"], 0)

    def test_risk_levels(self):
        self.assertEqual(script_map.risk("nlapiX();", 0)["level"], "high")  # 3 + 2
        self.assertEqual(script_map.risk("try { nlapiX(); } catch (e) {}", 0)["level"], "medium")
        self.assertEqual(script_map.risk("try { nlapiX(); } catch (e) {}", 3),
                         {"score": 4, "level": "high",
                          "reasons": ["SuiteScript 1.0", "touches 3 records"]})
        self.assertEqual(script_map.risk("x();", 3)["level"], "medium")

    def test_risk_in_collect_counts_records(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT.replace(
            "customrecord2326 (Sales Order Hold)", "customrecord2326, invoice"))
        node = self.scripts(self.collect_one(repo))["ue.js"]
        self.assertEqual(node["risk"]["reasons"], ["touches 3 records"])
        self.assertEqual(node["risk"]["level"], "low")

    # AC10 / AC15: output

    def test_script_tag_in_string_round_trips(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT.replace(
            "(Sales Order Hold)", "(Hold </script><!-- x --> Record)").replace(
            "'./SO Hold Creation'", "'./lib</script>'"))
        self.write("a/n.js", "var s = '</script>';\n")
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                      "--title", "Client </script> map"])
        self.assertEqual(code, 0, err)
        raw, data = self.read_data()
        self.assertNotIn("</", raw)
        self.assertNotIn("<!--", raw)
        self.assertEqual(data["title"], "Client </script> map")
        labels = {n["id"]: n["label"] for n in data["nodes"] if "label" in n}
        self.assertEqual(labels["A:customrecord2326"], "Hold </script><!-- x --> Record")
        self.assertEqual(labels["A:unresolved:./lib</script>"], "./lib</script>")
        self.assertIn("n.js", [s["path"] for s in data["skipped"]])
        self.assertTrue(data["generated_at"].endswith("Z"))

    def test_stdout_line(self):
        repo = self.make_repo("a")
        self.write("a/UserEvent/ue.js", USER_EVENT)
        self.write("a/Shared/SO Hold Creation.js", LIBRARY)
        self.write("a/plain.js", "x();\n")
        out_arg = str(self.out)
        code, out, _ = self.run_main(["--repo", f"A={repo}", "--out", out_arg])
        self.assertEqual(code, 0)
        self.assertEqual(out, f"script_map: 2 scripts, 2 records, 3 edges, 1 skipped -> {out_arg}\n")

    def test_overwrites_existing_output_and_creates_parents(self):
        repo = self.make_repo("a")
        out = self.root / "deep" / "er" / "map.html"
        out.parent.mkdir(parents=True)
        out.write_text("old", encoding="utf-8")
        code, _, _ = self.run_main(["--repo", f"A={repo}", "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertNotEqual(out.read_text(encoding="utf-8"), "old")
        code, _, _ = self.run_main(["--repo", f"A={repo}", "--out", str(self.root / "n" / "m.html")])
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "n" / "m.html").exists())

    def test_zero_scripts_still_writes_page(self):
        repo = self.make_repo("a")
        code, out, _ = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("script_map: 0 scripts, 0 records, 0 edges, 0 skipped"))
        _, data = self.read_data()
        self.assertEqual(data["nodes"], [])

    def test_invalid_utf8_does_not_crash(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT.encode("utf-8") + b"// caf\xe9 \xff\xfe\n")
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0, err)
        _, data = self.read_data()
        self.assertIn("ue.js", [n.get("path") for n in data["nodes"]])

    def test_subprocess_run(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        result = subprocess.run([sys.executable, str(SCRIPT), "--repo", f"A={repo}",
                                 "--out", str(self.out)], cwd=self.root,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"^script_map: 1 scripts, \d+ records, \d+ edges, "
                                        r"0 skipped -> .+map\.html\n$")

    # Summaries: header_description

    def test_header_description_tag_with_continuation(self):
        text = ("/**\n * @NApiVersion 2.1\n * @description Holds sales orders\n"
                " *   when credit   is low.\n * DEPLOY ON: salesorder\n *\n"
                " * @NScriptType UserEventScript\n */\n")
        self.assertEqual(script_map.header_description(text),
                         "Holds sales orders when credit is low.")

    def test_header_description_untagged_lines(self):
        text = ("// lead\n  /**\n   * Creates hold records.\n   ** Runs nightly.\n"
                "   * deploy on: invoice\n   *\n   * @NApiVersion 2.1\n   * Ignored.\n   */")
        self.assertEqual(script_map.header_description(text),
                         "Creates hold records. Runs nightly.")

    def test_header_description_deploy_only_and_no_block(self):
        self.assertEqual(script_map.header_description(USER_EVENT), "")
        self.assertEqual(script_map.header_description("/** DEPLOY ON: x */"), "")
        self.assertEqual(script_map.header_description(V1_SCRIPT), "")
        self.assertEqual(script_map.header_description("/* plain */ x();"), "")
        self.assertEqual(script_map.header_description("/** never closed"), "")

    def test_header_description_cut_at_300(self):
        long_line = " ".join(["word"] * 100)  # 499 characters
        result = script_map.header_description(f"/**\n * {long_line}\n */")
        self.assertEqual(result, long_line[:300] + "…")
        self.assertEqual(len(result), 301)
        exact = "x" * 300
        self.assertEqual(script_map.header_description(f"/** {exact} */"), exact)

    def test_crlf_and_lone_cr_give_same_nodes(self):
        repo = self.make_repo("a")
        lf = USER_EVENT.replace("/**\n", "/**\n * Holds orders.\n")
        self.write("a/lf/ue.js", lf)
        self.write("a/crlf/ue.js", lf.replace("\n", "\r\n"))
        self.write("a/cr/ue.js", lf.replace("\n", "\r"))
        self.write("a/bad/ue.js", lf.encode("utf-8") + b"// caf\xe9\n")
        scripts = self.scripts(self.collect_one(repo))

        def strip(node):
            return {k: v for k, v in node.items() if k not in ("id", "path", "summary_key")}

        base = strip(scripts["lf/ue.js"])
        self.assertEqual(base["summary"], "Holds orders.")
        self.assertEqual(base["summary_source"], "header")
        self.assertEqual(strip(scripts["crlf/ue.js"]), base)
        self.assertEqual(strip(scripts["cr/ue.js"]), base)
        bad = strip(scripts["bad/ue.js"])
        self.assertEqual(bad["entry_points"], ["beforeLoad"])
        self.assertEqual(bad["summary"], "Holds orders.")

    # Summaries: --summaries

    def write_cache(self, entries, name="cache.json"):
        path = self.root / name
        path.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")
        return path

    def test_summaries_ai_header_and_empty(self):
        repo = self.make_repo("a")
        ai = self.write("a/ai.js", USER_EVENT)
        self.write("a/stale.js", USER_EVENT.replace("/**\n", "/**\n * Header text.\n"))
        self.write("a/none.js", USER_EVENT + "// different content\n")
        sha = hashlib.sha256(ai.read_bytes()).hexdigest()
        cache = self.write_cache({
            "A:ai.js": {"sha256": sha, "summary": "Holds orders.", "model": "m",
                        "generated_at": "2026-01-01T00:00:00Z"},
            "A:stale.js": {"sha256": "0" * 64, "summary": "Old.", "model": "m",
                           "generated_at": "2026-01-01T00:00:00Z"},
        })
        code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                        "--summaries", str(cache)])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("script_map: 3 scripts, "))
        _, data = self.read_data()
        nodes = self.scripts(data)
        self.assertEqual((nodes["ai.js"]["summary"], nodes["ai.js"]["summary_source"]),
                         ("Holds orders.", "ai"))
        self.assertEqual((nodes["stale.js"]["summary"], nodes["stale.js"]["summary_source"]),
                         ("Header text.", "header"))
        self.assertEqual((nodes["none.js"]["summary"], nodes["none.js"]["summary_source"]),
                         ("", ""))

    def test_summaries_entry_for_other_label_with_other_sha_ignored(self):
        repo = self.make_repo("a")
        self.write("a/ai.js", USER_EVENT)
        entries = {"B:ai.js": {"sha256": "0" * 64, "summary": "Other.", "model": "m",
                               "generated_at": "2026-01-01T00:00:00Z"}}
        node = self.scripts(script_map.collect([("A", repo)], entries))["ai.js"]
        self.assertEqual(node["summary_source"], "")

    def summary_entry(self, sha, summary):
        return {"sha256": sha, "summary": summary, "model": "m",
                "generated_at": "2026-01-01T00:00:00Z"}

    def test_summaries_match_by_sha_under_other_key(self):
        repo = self.make_repo("a")
        ai = self.write("a/ai.js", USER_EVENT)
        sha = hashlib.sha256(ai.read_bytes()).hexdigest()
        for key in ("B:ai.js", "A:Moved/ai.js", "sandbox|A:ai.js"):
            with self.subTest(key=key):
                entries = {key: self.summary_entry(sha, "Shared.")}
                node = self.scripts(script_map.collect([("A", repo)], entries))["ai.js"]
                self.assertEqual((node["summary"], node["summary_source"]), ("Shared.", "ai"))

    def test_summaries_sha_fallback_first_in_key_order_and_ignores_malformed(self):
        repo = self.make_repo("a")
        ai = self.write("a/ai.js", USER_EVENT)
        sha = hashlib.sha256(ai.read_bytes()).hexdigest()
        entries = {
            "sandbox|A:ai.js": self.summary_entry(sha, "Later key."),
            "C:x.js": self.summary_entry(sha, "First key."),
            "0:bad.js": "not a dict",
            "1:nosummary.js": {"sha256": sha},
            "2:numbersha.js": {"sha256": 5, "summary": "Bad."},
            "A:ai.js": self.summary_entry("0" * 64, "Stale own key."),
        }
        node = self.scripts(script_map.collect([("A", repo)], entries))["ai.js"]
        self.assertEqual((node["summary"], node["summary_source"]), ("First key.", "ai"))

    def test_summaries_exact_key_wins_over_sha_match(self):
        repo = self.make_repo("a")
        ai = self.write("a/ai.js", USER_EVENT)
        sha = hashlib.sha256(ai.read_bytes()).hexdigest()
        entries = {"0:other.js": self.summary_entry(sha, "Other key."),
                   "A:ai.js": self.summary_entry(sha, "Own key.")}
        node = self.scripts(script_map.collect([("A", repo)], entries))["ai.js"]
        self.assertEqual((node["summary"], node["summary_source"]), ("Own key.", "ai"))

    def test_summary_generated_at_own_key(self):
        repo = self.make_repo("a")
        ai = self.write("a/ai.js", USER_EVENT)
        sha = hashlib.sha256(ai.read_bytes()).hexdigest()
        entries = {"0:other.js": dict(self.summary_entry(sha, "Other."),
                                      generated_at="2025-05-05T05:05:05Z"),
                   "A:ai.js": dict(self.summary_entry(sha, "Own."),
                                   generated_at="2026-02-03T04:05:06Z")}
        node = self.scripts(script_map.collect([("A", repo)], entries))["ai.js"]
        self.assertEqual((node["summary"], node["summary_source"], node["summary_generated_at"]),
                         ("Own.", "ai", "2026-02-03T04:05:06Z"))

    def test_summary_generated_at_from_sha_fallback_entry(self):
        repo = self.make_repo("a")
        ai = self.write("a/ai.js", USER_EVENT)
        sha = hashlib.sha256(ai.read_bytes()).hexdigest()
        entries = {
            "sandbox|A:ai.js": dict(self.summary_entry(sha, "Later key."),
                                    generated_at="2026-09-09T09:09:09Z"),
            "C:x.js": dict(self.summary_entry(sha, "First key."),
                           generated_at="2026-03-03T03:03:03Z"),
            "A:ai.js": dict(self.summary_entry("0" * 64, "Stale own key."),
                            generated_at="2026-12-12T12:12:12Z"),
        }
        node = self.scripts(script_map.collect([("A", repo)], entries))["ai.js"]
        self.assertEqual((node["summary"], node["summary_generated_at"]),
                         ("First key.", "2026-03-03T03:03:03Z"))

    def test_summary_generated_at_missing_or_not_string(self):
        repo = self.make_repo("a")
        ai = self.write("a/ai.js", USER_EVENT)
        sha = hashlib.sha256(ai.read_bytes()).hexdigest()
        for key in ("A:ai.js", "B:ai.js"):
            for value in (None, 5, ["2026-01-01T00:00:00Z"], "missing"):
                with self.subTest(key=key, value=value):
                    entry = {"sha256": sha, "summary": "S.", "model": "m"}
                    if value != "missing":
                        entry["generated_at"] = value
                    node = self.scripts(script_map.collect([("A", repo)], {key: entry}))["ai.js"]
                    self.assertEqual((node["summary_source"], node["summary_generated_at"]),
                                     ("ai", ""))

    def test_summary_generated_at_empty_for_header_and_empty(self):
        repo = self.make_repo("a")
        self.write("a/stale.js", USER_EVENT.replace("/**\n", "/**\n * Header text.\n"))
        self.write("a/none.js", USER_EVENT)
        entries = {"A:stale.js": self.summary_entry("0" * 64, "Old."),
                   "A:none.js": self.summary_entry("0" * 64, "Old.")}
        nodes = self.scripts(script_map.collect([("A", repo)], entries))
        self.assertEqual((nodes["stale.js"]["summary_source"],
                          nodes["stale.js"]["summary_generated_at"]), ("header", ""))
        self.assertEqual((nodes["none.js"]["summary_source"],
                          nodes["none.js"]["summary_generated_at"]), ("", ""))
        self.assertEqual(sorted(nodes["none.js"]), sorted([
            "id", "kind", "repo", "path", "name", "type", "type_inferred", "api_version",
            "version", "entry_points", "modules", "has_deploy_line", "risk", "summary",
            "summary_source", "summary_generated_at", "summary_key", "summary_trigger",
            "summary_author", "summary_guidance", "summary_history", "last_commit",
            "last_commit_author", "last_commit_date", "last_commit_subject", "last_commit_pr",
            "last_commit_url", "last_commit_pr_url", "commit_history"]))

    # Summary trigger, author, guidance and history; --team

    def summary_details(self, node):
        return (node["summary_trigger"], node["summary_author"], node["summary_guidance"],
                node["summary_history"])

    def history_item(self, summary, trigger="", author="", guidance=""):
        return {"summary": summary, "generated_at": "2026-01-01T00:00:00Z",
                "trigger": trigger, "author": author, "guidance": guidance}

    def test_summary_details_from_own_key(self):
        repo = self.make_repo("a")
        ai = self.write("a/ai.js", USER_EVENT)
        sha = hashlib.sha256(ai.read_bytes()).hexdigest()
        history = [self.history_item("Older.", "first")]
        entries = {"0:other.js": dict(self.summary_entry(sha, "Other."), trigger="reused"),
                   "A:ai.js": dict(self.summary_entry(sha, "Own."), trigger="regenerate",
                                   author="Kim", guidance="Mention holds.",
                                   history=history)}
        node = self.scripts(script_map.collect([("A", repo)], entries))["ai.js"]
        self.assertEqual(node["summary"], "Own.")
        self.assertEqual(self.summary_details(node),
                         ("regenerate", "Kim", "Mention holds.", history))

    def test_summary_details_from_sha_fallback_entry(self):
        repo = self.make_repo("a")
        ai = self.write("a/ai.js", USER_EVENT)
        sha = hashlib.sha256(ai.read_bytes()).hexdigest()
        history = [self.history_item("Before.", "code_changed")]
        entries = {
            "sandbox|A:ai.js": dict(self.summary_entry(sha, "Later key."), trigger="first"),
            "C:x.js": dict(self.summary_entry(sha, "First key."), trigger="regenerate",
                           author="Lee", guidance="G.", history=history),
            "A:ai.js": dict(self.summary_entry("0" * 64, "Stale own key."),
                            trigger="first", author="Own", guidance="Own G."),
        }
        node = self.scripts(script_map.collect([("A", repo)], entries))["ai.js"]
        self.assertEqual(node["summary"], "First key.")
        self.assertEqual(self.summary_details(node), ("regenerate", "Lee", "G.", history))

    def test_summary_details_empty_for_header_empty_and_legacy(self):
        repo = self.make_repo("a")
        ai = self.write("a/ai.js", USER_EVENT.replace("DEPLOY ON", "// x\n * DEPLOY ON"))
        self.write("a/stale.js", USER_EVENT.replace("/**\n", "/**\n * Header text.\n"))
        self.write("a/none.js", USER_EVENT)
        sha = hashlib.sha256(ai.read_bytes()).hexdigest()
        full = {"trigger": "regenerate", "author": "Kim", "guidance": "G.",
                "history": [self.history_item("Old.")]}
        entries = {"A:ai.js": self.summary_entry(sha, "Legacy."),
                   "A:stale.js": dict(self.summary_entry("0" * 64, "Old."), **full),
                   "A:none.js": dict(self.summary_entry("0" * 64, "Old."), **full)}
        nodes = self.scripts(script_map.collect([("A", repo)], entries))
        self.assertEqual(nodes["ai.js"]["summary_source"], "ai")
        self.assertEqual(nodes["stale.js"]["summary_source"], "header")
        self.assertEqual(nodes["none.js"]["summary_source"], "")
        for name in ("ai.js", "stale.js", "none.js"):
            with self.subTest(name=name):
                self.assertEqual(self.summary_details(nodes[name]), ("", "", "", []))
        no_cache = self.scripts(self.collect_one(repo))["none.js"]
        self.assertEqual(self.summary_details(no_cache), ("", "", "", []))

    def test_summary_details_empty_for_empty_cached_summary(self):
        repo = self.make_repo("a")
        own = self.write("a/own.js", USER_EVENT)
        moved = self.write("a/moved.js", USER_EVENT.replace("DEPLOY ON", "// x\n * DEPLOY ON"))
        own_sha = hashlib.sha256(own.read_bytes()).hexdigest()
        moved_sha = hashlib.sha256(moved.read_bytes()).hexdigest()
        full = {"trigger": "regenerate", "author": "Kim", "guidance": "G.",
                "history": [self.history_item("Old.", "first")]}
        entries = {"A:own.js": dict(self.summary_entry(own_sha, ""), **full),
                   "B:moved.js": dict(self.summary_entry(moved_sha, ""), **full),
                   "A:moved.js": dict(self.summary_entry("0" * 64, "Stale."), **full)}
        nodes = self.scripts(script_map.collect([("A", repo)], entries))
        for name in ("own.js", "moved.js"):
            with self.subTest(name=name):
                self.assertEqual(nodes[name]["summary"], "")
                self.assertEqual(self.summary_details(nodes[name]), ("", "", "", []))

    def test_summary_details_non_string_and_malformed_history(self):
        repo = self.make_repo("a")
        ai = self.write("a/ai.js", USER_EVENT)
        sha = hashlib.sha256(ai.read_bytes()).hexdigest()
        history = ["not a dict", None, 5, ["list"],
                   {"summary": "One.", "generated_at": 7, "trigger": None, "extra": "x"},
                   {}]
        history += [self.history_item(f"H{i}.") for i in range(6)]
        entry = dict(self.summary_entry(sha, "S."), trigger=5, author=None,
                     guidance=["g"], history=history)
        node = self.scripts(script_map.collect([("A", repo)], {"A:ai.js": entry}))["ai.js"]
        empty = {"summary": "", "generated_at": "", "trigger": "", "author": "",
                 "guidance": ""}
        self.assertEqual(self.summary_details(node), ("", "", "", [
            dict(empty, summary="One."), empty, self.history_item("H0."),
            self.history_item("H1."), self.history_item("H2.")]))
        for value in ("not a list", {"a": 1}, None, 5):
            with self.subTest(history=value):
                entry = dict(self.summary_entry(sha, "S."), history=value)
                node = self.scripts(
                    script_map.collect([("A", repo)], {"A:ai.js": entry}))["ai.js"]
                self.assertEqual(node["summary_history"], [])

    def test_summary_details_via_cli(self):
        repo = self.make_repo("a")
        ai = self.write("a/ai.js", USER_EVENT)
        sha = hashlib.sha256(ai.read_bytes()).hexdigest()
        history = [self.history_item("Old.", "first")]
        cache = self.write_cache({"A:ai.js": dict(
            self.summary_entry(sha, "New."), trigger="regenerate", author="Kim",
            guidance="G.", history=history)})
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                      "--summaries", str(cache)])
        self.assertEqual(code, 0, err)
        node = self.scripts(self.read_data()[1])["ai.js"]
        self.assertEqual(self.summary_details(node), ("regenerate", "Kim", "G.", history))

    def test_guidance_with_script_tag_stays_escaped(self):
        repo = self.make_repo("a")
        ai = self.write("a/ai.js", USER_EVENT)
        sha = hashlib.sha256(ai.read_bytes()).hexdigest()
        guidance = "Say </script><script>alert(1)</script> <!-- x"
        cache = self.write_cache({"A:ai.js": dict(
            self.summary_entry(sha, "New."), trigger="regenerate", author="</script>",
            guidance=guidance,
            history=[self.history_item("Old.", "regenerate", "Kim", guidance)])})
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                      "--summaries", str(cache), "--team", "A </script>"])
        self.assertEqual(code, 0, err)
        raw, data = self.read_data()
        self.assertNotIn("</", raw)
        self.assertNotIn("<!--", raw)
        node = self.scripts(data)["ai.js"]
        self.assertEqual(node["summary_guidance"], guidance)
        self.assertEqual(node["summary_author"], "</script>")
        self.assertEqual(node["summary_history"][0]["guidance"], guidance)
        self.assertEqual(data["team"], ["A </script>"])
        page = self.out.read_text(encoding="utf-8")
        self.assertEqual(page.count("</script>"), 2)

    def test_team_sorted_deduplicated_and_trimmed(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        code, out, err = self.run_main([
            "--repo", f"A={repo}", "--out", str(self.out),
            "--team", " kim ", "--team", "Alex", "--team", "bo", "--team", "Kim",
            "--team", "kim", "--team", "x" * 40, "--team", "Zoë Smith"])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("script_map: "))
        _, data = self.read_data()
        self.assertEqual(data["team"], ["Alex", "bo", "Kim", "kim", "x" * 40, "Zoë Smith"])

    def test_team_absent_is_empty_list(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0, err)
        _, data = self.read_data()
        self.assertEqual(data["team"], [])

    def test_team_invalid_exits_2(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        for value in ("", "   ", "x" * 41, "Kim\x07", "Kim\nLee", "\tKim\x00"):
            with self.subTest(value=value):
                code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                                "--team", "Kim", "--team", value])
                self.assertEqual(code, 2)
                self.assertEqual(err, "error: --team must be 1-40 characters\n")
                self.assertEqual(out, "")
                self.assertFalse(self.out.exists())

    def test_team_checked_before_repo_scan(self):
        code, out, err = self.run_main(["--repo", f"A={self.root / 'missing'}",
                                        "--out", str(self.out), "--team", ""])
        self.assertEqual(code, 2)
        self.assertEqual(err, "error: --team must be 1-40 characters\n")
        self.assertFalse(self.out.exists())

    def test_page_top_level_keys(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0, err)
        _, data = self.read_data()
        self.assertEqual(sorted(data), sorted([
            "repos", "nodes", "edges", "skipped", "record_categories", "title", "refresh_url",
            "environment", "switch_url", "switch_label", "missing_repos", "branch", "team",
            "generated_at", "link_origin"]))

    def test_help_mentions_team(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--team", result.stdout)

    def test_summaries_sha_fallback_via_cli(self):
        repo = self.make_repo("a", "ref: refs/heads/sandbox\n")
        ai = self.write("a/ai.js", USER_EVENT)
        sha = hashlib.sha256(ai.read_bytes()).hexdigest()
        cache = self.write_cache({"A:ai.js": self.summary_entry(sha, "Prod summary.")})
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                      "--branch", "sandbox", "--summaries", str(cache)])
        self.assertEqual(code, 0, err)
        node = self.scripts(self.read_data()[1])["ai.js"]
        self.assertEqual((node["summary"], node["summary_source"]), ("Prod summary.", "ai"))

    def test_summaries_invalid_or_missing_cache(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        bad_json = self.root / "bad.json"
        bad_json.write_text("{not json", encoding="utf-8")
        wrong_version = self.root / "v2.json"
        wrong_version.write_text(json.dumps({"version": 2, "entries": {}}), encoding="utf-8")
        no_entries = self.root / "list.json"
        no_entries.write_text(json.dumps({"version": 1, "entries": []}), encoding="utf-8")
        for path in (bad_json, wrong_version, no_entries):
            with self.subTest(path=path.name):
                code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                                "--summaries", str(path)])
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(err.strip(), f"error: {path} is not a valid summary cache")
                self.assertFalse(self.out.exists())
        missing = self.root / "missing.json"
        code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                        "--summaries", str(missing)])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err.strip(), f"error: {missing} does not exist")
        self.assertFalse(self.out.exists())

    def test_summary_key_without_namespace(self):
        repo = self.make_repo("a")
        self.write("a/Sub/ue.js", USER_EVENT)
        code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("script_map: 1 scripts, "))
        node = self.scripts(self.read_data()[1])["Sub/ue.js"]
        self.assertEqual(node["summary_key"], "A:Sub/ue.js")
        self.assertEqual(self.scripts(self.collect_one(repo))["Sub/ue.js"]["summary_key"],
                         "A:Sub/ue.js")

    def test_summary_key_with_namespace(self):
        repo = self.make_repo("a", "ref: refs/heads/sandbox\n")
        self.write("a/Sub/ue.js", USER_EVENT)
        code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                        "--branch", "sandbox",
                                        "--summary-namespace", "sandbox"])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("script_map: 1 scripts, "))
        node = self.scripts(self.read_data()[1])["Sub/ue.js"]
        self.assertEqual(node["id"], "A:Sub/ue.js")
        self.assertEqual(node["summary_key"], "sandbox|A:Sub/ue.js")

    def test_summary_namespace_does_not_change_summary_lookup(self):
        repo = self.make_repo("a")
        ai = self.write("a/ai.js", USER_EVENT)
        sha = hashlib.sha256(ai.read_bytes()).hexdigest()
        entries = {"A:ai.js": self.summary_entry(sha, "Own key.")}
        node = self.scripts(script_map.collect([("A", repo)], entries,
                                               summary_namespace="sandbox"))["ai.js"]
        self.assertEqual((node["summary"], node["summary_source"]), ("Own key.", "ai"))
        self.assertEqual(node["summary_key"], "sandbox|A:ai.js")

    def test_summary_namespace_invalid(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        for value in ("sand box", "a|b", "a:b", "x/y"):
            with self.subTest(value=value):
                code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                                "--summary-namespace", value])
                self.assertEqual(code, 2)
                self.assertEqual(
                    err, "error: --summary-namespace must be letters, digits, _ or -\n")
                self.assertEqual(out, "")
                self.assertFalse(self.out.exists())

    def test_help_mentions_summary_namespace(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--summary-namespace", result.stdout)

    def test_help_mentions_summaries(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--summaries", result.stdout)

    # Git last change: --git-info

    def write_git_info(self, files, name="gitinfo.json", version=1, repos=None):
        path = self.root / name
        info = {"version": version, "generated_at": "2026-01-01T00:00:00Z", "files": files}
        if repos is not None:
            info["repos"] = repos
        path.write_text(json.dumps(info), encoding="utf-8")
        return path

    def test_help_mentions_git_info(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--git-info", result.stdout)

    def test_git_info_missing_file(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        missing = self.root / "nope.json"
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                      "--git-info", str(missing)])
        self.assertEqual(code, 2)
        self.assertIn(f"error: {missing} does not exist", err)
        self.assertFalse(self.out.exists())

    def test_git_info_invalid_files(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        cases = {
            "bad_json": "{not json",
            "wrong_version": json.dumps({"version": 2, "files": {}}),
            "files_list": json.dumps({"version": 1, "files": []}),
            "files_missing": json.dumps({"version": 1}),
            "not_object": json.dumps([1]),
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                path = self.root / f"{name}.json"
                path.write_text(text, encoding="utf-8")
                code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                              "--git-info", str(path)])
                self.assertEqual(code, 2)
                self.assertIn(f"error: {path} is not a valid git info file", err)
                self.assertFalse(self.out.exists())

    def test_git_info_fields_on_script_nodes(self):
        repo = self.make_repo("a")
        self.write("a/has.js", USER_EVENT)
        self.write("a/Sub/none.js", USER_EVENT)
        self.write("a/odd.js", USER_EVENT)
        self.write("a/bad.js", USER_EVENT)
        info = self.write_git_info({
            "A:has.js": {"commit": "a" * 40, "author": "Zoë Ann", "date": "2026-03-01T17:00:00Z"},
            "A:odd.js": {"commit": 5, "author": None, "date": "2026-03-01T17:00:00Z"},
            "A:bad.js": "not an object",
            "A:gone.js": {"commit": "b" * 40, "author": "X", "date": "2026-01-01T00:00:00Z"},
        })
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                      "--git-info", str(info)])
        self.assertEqual(code, 0, err)
        nodes = self.scripts(self.read_data()[1])

        def last(node):
            return (node["last_commit"], node["last_commit_author"], node["last_commit_date"])

        self.assertEqual(last(nodes["has.js"]), ("a" * 40, "Zoë Ann", "2026-03-01T17:00:00Z"))
        self.assertEqual(last(nodes["Sub/none.js"]), ("", "", ""))
        self.assertEqual(last(nodes["odd.js"]), ("", "", "2026-03-01T17:00:00Z"))
        self.assertEqual(last(nodes["bad.js"]), ("", "", ""))
        self.assertNotIn("gone.js", nodes)

    def test_git_info_absent_gives_empty_fields(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0, err)
        node = self.scripts(self.read_data()[1])["ue.js"]
        self.assertEqual((node["last_commit"], node["last_commit_author"],
                          node["last_commit_date"]), ("", "", ""))
        node = self.scripts(self.collect_one(repo))["ue.js"]
        self.assertEqual((node["last_commit"], node["last_commit_author"],
                          node["last_commit_date"]), ("", "", ""))

    def test_git_info_key_uses_label(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        files = {"B:ue.js": {"commit": "c" * 40, "author": "Kim", "date": "2026-01-01T00:00:00Z"}}
        node = self.scripts(script_map.collect([("A", repo)], git_info=files))["ue.js"]
        self.assertEqual(node["last_commit_author"], "")
        node = self.scripts(script_map.collect([("B", repo)], git_info=files))["ue.js"]
        self.assertEqual(node["last_commit_author"], "Kim")

    LAST_CHANGE_TAIL = """
var out = {};
scriptEntries().forEach(function (d) {
    var dl = walk(d, function (c) { return c.tagName === 'dl'; })[0];
    var labels = dl.children.map(function (c) { return c.tagName === 'dt' ? c.textContent : null; });
    var i = labels.indexOf('Last change');
    var dd = dl.children[i + 1];
    var value = dd.children[0];
    out[entryName(d)] = {
        after: dl.children[i - 2].textContent,
        text: dd.textContent,
        cls: value && value.className ? value.className : ''
    };
});
console.log(JSON.stringify(out));
"""

    def test_last_change_full_data_from_git_info(self):
        info = self.write_git_info({"RTMH:ue.js": {
            "commit": "0123456789abcdef0123456789abcdef01234567", "author": "Zoë Ann",
            "date": "2026-03-01T17:05:09Z"}})
        result = self.run_header_in_node(["--git-info", str(info)], self.LAST_CHANGE_TAIL)
        self.assertEqual(result, {"ue.js": {
            "after": "Summary", "text": "Zoë Ann · 2026-03-01 17:05 UTC · 0123456", "cls": ""}})

    def test_last_change_partial_and_missing_data(self):
        repos = {"RTMH": {n: USER_EVENT for n in ("a.js", "b.js", "c.js", "d.js", "e.js",
                                                   "f.js", "g.js")}}
        values = {
            "a.js": ["Kim", "", ""],
            "b.js": ["", "2026-03-01T17:05:09Z", ""],
            "c.js": ["", "2026-03-01T10:05:09-07:00", "abcdef0123"],
            "d.js": ["Kim", "not a date", "abcdef0123"],
            "e.js": ["", "", "abcdef0123"],
            "f.js": ["", "", ""],
        }
        prelude = ("editData(function (d) { var v = %s; d.nodes.forEach(function (n) {"
                   " if (v[n.name]) { n.last_commit_author = v[n.name][0];"
                   " n.last_commit_date = v[n.name][1]; n.last_commit = v[n.name][2]; }"
                   " if (n.name === 'g.js') { delete n.last_commit_author;"
                   " n.last_commit_date = 7; n.last_commit = null; } }); });"
                   % json.dumps(values))
        result = self.run_header_in_node([], self.LAST_CHANGE_TAIL, prelude, repos)
        unknown = {"after": "Summary", "text": "Unknown", "cls": "empty"}
        self.assertEqual(result, {
            "a.js": {"after": "Summary", "text": "Kim", "cls": ""},
            "b.js": {"after": "Summary", "text": "2026-03-01 17:05 UTC", "cls": ""},
            "c.js": {"after": "Summary", "text": "2026-03-01 17:05 UTC · abcdef0", "cls": ""},
            "d.js": {"after": "Summary", "text": "Kim · abcdef0", "cls": ""},
            "e.js": unknown,
            "f.js": unknown,
            "g.js": unknown,
        })

    # GitHub links: subject, PR, commit history and URLs

    SHA_A = "0123456789abcdef0123456789abcdef01234567"
    SHA_B = "89abcdef0123456789abcdef0123456789abcdef"
    SHA_C = "fedcba9876543210fedcba9876543210fedcba98"

    def commit_entry(self, sha, author, date, subject, pr, history=None):
        entry = {"commit": sha, "author": author, "date": date, "subject": subject, "pr": pr}
        if history is not None:
            entry["history"] = history
        return entry

    def three_commits(self):
        return [
            self.commit_entry(self.SHA_A, "Zoë Ann", "2026-03-03T17:05:09Z",
                              "Fix deposit calc (#12)", "12"),
            self.commit_entry(self.SHA_B, "Kim", "2026-03-02T09:00:00Z", "Tidy", ""),
            self.commit_entry(self.SHA_C, "Bo", "2026-03-01T08:00:00Z", "Start (#7)", "7"),
        ]

    def test_link_origin_on_every_page(self):
        self.assertEqual(script_map.LINK_ORIGIN, "https://github.com/")
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        info = self.write_git_info({}, repos={"A": "o/r"})
        for extra in ([], ["--git-info", str(info)], ["--branch", "main"]):
            with self.subTest(extra=extra):
                code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)]
                                             + extra)
                self.assertEqual(code, 0, err)
                self.assertEqual(self.read_data()[1]["link_origin"], "https://github.com/")

    def test_commit_links_and_history_urls(self):
        repo = self.make_repo("a")
        self.write("a/Client Scripts/a b.js", USER_EVENT)
        history = self.three_commits()
        files = {"A:Client Scripts/a b.js": dict(history[0], history=history)}
        node = self.scripts(script_map.collect([("A", repo)], git_info=files,
                                               git_repos={"A": "o/r"}))["Client Scripts/a b.js"]
        base = "https://github.com/o/r/"
        self.assertEqual(node["last_commit"], self.SHA_A)
        self.assertEqual(node["last_commit_author"], "Zoë Ann")
        self.assertEqual(node["last_commit_date"], "2026-03-03T17:05:09Z")
        self.assertEqual(node["last_commit_subject"], "Fix deposit calc (#12)")
        self.assertEqual(node["last_commit_pr"], "12")
        self.assertEqual(node["last_commit_url"], base + "commit/" + self.SHA_A)
        self.assertEqual(node["last_commit_pr_url"], base + "pull/12")
        self.assertEqual(len(node["commit_history"]), 3)
        self.assertEqual(node["commit_history"][0], {
            "commit": self.SHA_A, "author": "Zoë Ann", "date": "2026-03-03T17:05:09Z",
            "subject": "Fix deposit calc (#12)", "pr": "12",
            "commit_url": base + "commit/" + self.SHA_A,
            "pr_url": base + "pull/12",
            "file_url": base + "blob/" + self.SHA_A + "/Client%20Scripts/a%20b.js",
            "raw_url": base + "raw/" + self.SHA_A + "/Client%20Scripts/a%20b.js",
        })
        self.assertEqual(node["commit_history"][1]["pr_url"], "")
        self.assertEqual(node["commit_history"][1]["file_url"],
                         base + "blob/" + self.SHA_B + "/Client%20Scripts/a%20b.js")
        self.assertEqual(node["commit_history"][2]["pr_url"], base + "pull/7")

    def test_commit_links_via_main(self):
        repo = self.make_repo("a")
        self.write("a/Client Scripts/a b.js", USER_EVENT)
        history = self.three_commits()
        info = self.write_git_info({"A:Client Scripts/a b.js": dict(history[0], history=history)},
                                   repos={"A": "o/r"})
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                      "--git-info", str(info)])
        self.assertEqual(code, 0, err)
        node = self.scripts(self.read_data()[1])["Client Scripts/a b.js"]
        self.assertEqual(node["last_commit_url"], "https://github.com/o/r/commit/" + self.SHA_A)
        self.assertEqual(node["commit_history"][0]["raw_url"],
                         "https://github.com/o/r/raw/" + self.SHA_A + "/Client%20Scripts/a%20b.js")
        self.assertEqual([h["commit"] for h in node["commit_history"]],
                         [self.SHA_A, self.SHA_B, self.SHA_C])

    def test_special_characters_in_path_are_quoted(self):
        repo = self.make_repo("a")
        self.write("a/Sub/Ordér #1?.js", USER_EVENT)
        files = {"A:Sub/Ordér #1?.js": dict(self.three_commits()[0],
                                            history=self.three_commits()[:1])}
        node = self.scripts(script_map.collect([("A", repo)], git_info=files,
                                               git_repos={"A": "o/r"}))["Sub/Ordér #1?.js"]
        self.assertEqual(node["commit_history"][0]["file_url"],
                         "https://github.com/o/r/blob/" + self.SHA_A
                         + "/Sub/Ord%C3%A9r%20%231%3F.js")

    def test_invalid_slug_commit_and_pr_blank_only_affected_urls(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)

        def node_for(entry, slug):
            files = {"A:ue.js": dict(entry, history=[entry])}
            return self.scripts(script_map.collect([("A", repo)], git_info=files,
                                                   git_repos={"A": slug}))["ue.js"]

        good = self.three_commits()[0]
        for slug in ("o", "o/r/x", "../r?", "", "o/r#x", " o/r"):
            with self.subTest(slug=slug):
                node = node_for(good, slug)
                item = node["commit_history"][0]
                self.assertEqual((node["last_commit_url"], node["last_commit_pr_url"]), ("", ""))
                self.assertEqual((item["commit_url"], item["pr_url"], item["file_url"],
                                  item["raw_url"]), ("", "", "", ""))
                self.assertEqual((node["last_commit"], node["last_commit_pr"]),
                                 (self.SHA_A, "12"))
        for commit in ("abc123", "g" * 40, self.SHA_A + "0", "javascript:alert(1)"):
            with self.subTest(commit=commit):
                node = node_for(dict(good, commit=commit), "o/r")
                item = node["commit_history"][0]
                self.assertEqual(node["last_commit_url"], "")
                self.assertEqual((item["commit_url"], item["file_url"], item["raw_url"]),
                                 ("", "", ""))
                self.assertEqual(node["last_commit_pr_url"], "https://github.com/o/r/pull/12")
                self.assertEqual(item["pr_url"], "https://github.com/o/r/pull/12")
        for pr in ("12a", "1234567890", "-1", "#12", ""):
            with self.subTest(pr=pr):
                node = node_for(dict(good, pr=pr), "o/r")
                item = node["commit_history"][0]
                self.assertEqual((node["last_commit_pr_url"], item["pr_url"]), ("", ""))
                self.assertEqual(node["last_commit_url"],
                                 "https://github.com/o/r/commit/" + self.SHA_A)
                self.assertEqual(item["commit_url"], "https://github.com/o/r/commit/" + self.SHA_A)

    def test_repos_and_history_malformed_values(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        good = self.three_commits()[0]
        many = [dict(good) for _ in range(7)]
        cases = [
            ("not a list", []),
            (None, []),
            ([1, "x", None, {"commit": 5, "author": ["x"]}], [{
                "commit": "", "author": "", "date": "", "subject": "", "pr": "",
                "commit_url": "", "pr_url": "", "file_url": "", "raw_url": ""}]),
            (many, None),
        ]
        for history, expected in cases:
            with self.subTest(history=history):
                files = {"A:ue.js": dict(good, history=history)}
                node = self.scripts(script_map.collect([("A", repo)], git_info=files,
                                                       git_repos={"A": "o/r"}))["ue.js"]
                if expected is None:
                    self.assertEqual(len(node["commit_history"]), 5)
                else:
                    self.assertEqual(node["commit_history"], expected)
        for repos in ([1], "o/r", None, {"A": 5}, {"B": "o/r"}):
            with self.subTest(repos=repos):
                info = self.write_git_info({"A:ue.js": dict(good, history=[good])}, repos=repos)
                code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                              "--git-info", str(info)])
                self.assertEqual(code, 0, err)
                node = self.scripts(self.read_data()[1])["ue.js"]
                self.assertEqual((node["last_commit_url"], node["last_commit_pr_url"]), ("", ""))
                self.assertEqual(node["commit_history"][0]["commit_url"], "")
                self.assertEqual(node["last_commit_subject"], "Fix deposit calc (#12)")
        files = {"A:ue.js": dict(good, subject=5, pr=None)}
        node = self.scripts(script_map.collect([("A", repo)], git_info=files,
                                               git_repos={"A": "o/r"}))["ue.js"]
        self.assertEqual((node["last_commit_subject"], node["last_commit_pr"],
                          node["last_commit_pr_url"]), ("", "", ""))

    def test_old_format_git_info_loads_unchanged(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        self.write("a/none.js", USER_EVENT)
        info = self.write_git_info({"A:ue.js": {"commit": "a" * 40, "author": "Zoë Ann",
                                                "date": "2026-03-01T17:00:00Z"}})
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                      "--git-info", str(info)])
        self.assertEqual(code, 0, err)
        nodes = self.scripts(self.read_data()[1])
        for name, last in (("ue.js", ("a" * 40, "Zoë Ann", "2026-03-01T17:00:00Z")),
                           ("none.js", ("", "", ""))):
            with self.subTest(name=name):
                node = nodes[name]
                self.assertEqual((node["last_commit"], node["last_commit_author"],
                                  node["last_commit_date"]), last)
                self.assertEqual((node["last_commit_subject"], node["last_commit_pr"],
                                  node["last_commit_url"], node["last_commit_pr_url"],
                                  node["commit_history"]), ("", "", "", "", []))

    def test_script_map_never_runs_git(self):
        text = (TOOLS_DIR / "script_map.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", text)

    CHANGE_TAIL = """
function linkInfo(c) {
    return { tag: c.tagName, text: c.textContent, href: c.getAttribute('href'),
             target: c.getAttribute('target'), rel: c.getAttribute('rel') };
}
function tagged(root, tags) {
    return walk(root, function (c) { return tags.indexOf(c.tagName) !== -1; });
}
var out = {};
scriptEntries().forEach(function (d) {
    var dl = walk(d, function (c) { return c.tagName === 'dl'; })[0];
    var labels = dl.children.map(function (c) { return c.tagName === 'dt' ? c.textContent : null; });
    var dd = dl.children[labels.indexOf('Last change') + 1];
    var top = dd.children[0];
    var box = walk(dd, function (c) { return c.className === 'change-history'; })[0];
    var history = null;
    if (box) {
        var list = box.children[1];
        history = {
            tag: box.tagName,
            open: box.getAttribute('open'),
            summary: box.children[0].tagName + ':' + box.children[0].textContent,
            list_tag: list.tagName,
            items: list.children.map(function (li) {
                var row = li.children.filter(function (c) { return c.className === 'change-links'; })[0];
                return {
                    tag: li.tagName,
                    line: li.children[0].textContent,
                    line_links: tagged(li.children[0], ['a', 'span']).map(linkInfo),
                    subject: li.children.filter(function (c) { return c.className === 'kind'; })
                        .map(function (c) { return c.textContent; }),
                    links: row ? tagged(row, ['a', 'span']).map(linkInfo) : null
                };
            })
        };
    }
    out[entryName(d)] = {
        line: top.children[0] ? top.children[0].textContent : dd.textContent,
        line_links: top.children[0] ? tagged(top.children[0], ['a', 'span']).map(linkInfo) : [],
        subject: top.children.filter(function (c) { return c.className === 'kind'; })
            .map(function (c) { return c.textContent; }),
        history: history
    };
});
console.log(JSON.stringify(out));
"""

    def link(self, text, href):
        if href is None:
            return {"tag": "span", "text": text, "href": None, "target": None, "rel": None}
        return {"tag": "a", "text": text, "href": href, "target": "_blank",
                "rel": "noopener noreferrer"}

    def test_last_change_links_and_change_history_render(self):
        history = self.three_commits()
        info = self.write_git_info({"RTMH:ue.js": dict(history[0], history=history)},
                                   repos={"RTMH": "o/r"})
        result = self.run_header_in_node(["--git-info", str(info)], self.CHANGE_TAIL)["ue.js"]
        base = "https://github.com/o/r/"
        self.assertEqual(result["line"], "Zoë Ann · 2026-03-03 17:05 UTC · 0123456 · PR #12")
        self.assertEqual(result["line_links"], [
            self.link("0123456", base + "commit/" + self.SHA_A),
            self.link("PR #12", base + "pull/12"),
        ])
        self.assertEqual(result["subject"], ["Fix deposit calc (#12)"])
        box = result["history"]
        self.assertEqual((box["tag"], box["open"], box["summary"], box["list_tag"]),
                         ("details", None, "summary:Change history (3)", "ol"))
        items = box["items"]
        self.assertEqual([item["tag"] for item in items], ["li", "li", "li"])
        self.assertEqual([item["line"] for item in items], [
            "2026-03-03 17:05 UTC · Zoë Ann · 0123456 · PR #12 · current",
            "2026-03-02 09:00 UTC · Kim · 89abcde",
            "2026-03-01 08:00 UTC · Bo · fedcba9 · PR #7",
        ])
        self.assertEqual([item["line"].endswith(" · current") for item in items],
                         [True, False, False])
        self.assertEqual(items[0]["line_links"], [
            self.link("0123456", base + "commit/" + self.SHA_A),
            self.link("PR #12", base + "pull/12"),
        ])
        self.assertEqual([item["subject"] for item in items],
                         [["Fix deposit calc (#12)"], ["Tidy"], ["Start (#7)"]])
        for item, sha in zip(items, (self.SHA_A, self.SHA_B, self.SHA_C)):
            self.assertEqual(item["links"], [
                self.link("View file", base + "blob/" + sha + "/ue.js"),
                self.link("Raw", base + "raw/" + sha + "/ue.js"),
            ])

    def test_unsafe_or_missing_urls_render_as_text(self):
        history = self.three_commits()
        info = self.write_git_info({"RTMH:ue.js": dict(history[0], history=history)},
                                   repos={"RTMH": "o/r"})
        prelude = ("editData(function (d) { d.nodes.forEach(function (n) {"
                   " if (n.kind !== 'script') { return; }"
                   " n.last_commit_url = 'javascript:alert(1)';"
                   " n.last_commit_pr_url = 'https://github.com/';"
                   " n.commit_history[0].commit_url = 'javascript:alert(1)';"
                   " n.commit_history[0].pr_url = 5;"
                   " n.commit_history[0].file_url = '';"
                   " n.commit_history[0].raw_url = 'https://evil.example/x';"
                   " n.commit_history[1].raw_url = '';"
                   " }); });")
        result = self.run_header_in_node(["--git-info", str(info)], self.CHANGE_TAIL,
                                         prelude)["ue.js"]
        base = "https://github.com/o/r/"
        self.assertEqual(result["line_links"], [self.link("0123456", None),
                                                self.link("PR #12", None)])
        items = result["history"]["items"]
        self.assertEqual(items[0]["line_links"], [self.link("0123456", None),
                                                  self.link("PR #12", None)])
        self.assertIsNone(items[0]["links"])
        self.assertEqual(items[1]["links"],
                         [self.link("View file", base + "blob/" + self.SHA_B + "/ue.js")])
        self.assertEqual(items[2]["line_links"][0],
                         self.link("fedcba9", base + "commit/" + self.SHA_C))

    def test_no_link_origin_renders_text_only(self):
        history = self.three_commits()
        info = self.write_git_info({"RTMH:ue.js": dict(history[0], history=history)},
                                   repos={"RTMH": "o/r"})
        prelude = "editData(function (d) { delete d.link_origin; });"
        result = self.run_header_in_node(["--git-info", str(info)], self.CHANGE_TAIL,
                                         prelude)["ue.js"]
        self.assertEqual(result["line_links"], [self.link("0123456", None),
                                                self.link("PR #12", None)])
        self.assertTrue(all(item["links"] is None for item in result["history"]["items"]))

    def test_one_history_item_renders_no_change_history(self):
        history = self.three_commits()[:1]
        info = self.write_git_info({"RTMH:ue.js": dict(history[0], history=history)},
                                   repos={"RTMH": "o/r"})
        result = self.run_header_in_node(["--git-info", str(info)], self.CHANGE_TAIL)["ue.js"]
        self.assertIsNone(result["history"])
        self.assertEqual(result["line"], "Zoë Ann · 2026-03-03 17:05 UTC · 0123456 · PR #12")
        self.assertEqual(result["subject"], ["Fix deposit calc (#12)"])

    def test_last_change_without_pr_or_subject(self):
        entry = self.commit_entry(self.SHA_B, "Kim", "2026-03-02T09:00:00Z", "", "")
        info = self.write_git_info({"RTMH:ue.js": dict(entry, history=[entry])},
                                   repos={"RTMH": "o/r"})
        result = self.run_header_in_node(["--git-info", str(info)], self.CHANGE_TAIL)["ue.js"]
        self.assertEqual(result["line"], "Kim · 2026-03-02 09:00 UTC · 89abcde")
        self.assertEqual(result["line_links"], [
            self.link("89abcde", "https://github.com/o/r/commit/" + self.SHA_B)])
        self.assertEqual(result["subject"], [])
        self.assertIsNone(result["history"])

    def test_nested_details_and_link_css(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("#i-list details details, #rec-list details details {\n"
                      "    border: 0;\n    border-radius: 0;\n    background: none;\n"
                      "    padding: 0;\n    margin: 4px 0 0;\n}", text)
        self.assertIn("#i-list a, #rec-list a { color: var(--accent); }", text)
        self.assertIn(".change-history { margin-top: 4px; overflow-wrap: anywhere;", text)
        self.assertIn("var linkOrigin = String(data.link_origin || '');", text)
        self.assertIn("Change history (", text)
        self.assertEqual(text.count("<script"), 2)

    # AC10 / AC16: template

    def test_template_is_self_contained_and_safe(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        for bad in ("http://", "https://", "<link", "innerHTML", "outerHTML",
                    "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(bad, text)
        self.assertEqual(text.count(script_map.DATA_TOKEN), 1)
        for label in ("Inventory", "Records", "Risk grid", "All clients",
                      "All types", "All versions", "Clear all", "Total scripts",
                      "Pick an area to see its record types and scripts.", "In this area:",
                      "Other scripts that touch this record", "No scripts found",
                      "Order within a step follows deployment order in NetSuite, "
                      "which isn't in the code.", "Search scripts", "Light mode", "Dark mode",
                      "script-map-theme",
                      "Summary", "AI summary", "From header comment", "No description",
                      "Last updated", "Refresh data", "Refresh requested.",
                      "Refresh failed (HTTP ", "Refresh failed (network error).",
                      "Try again after", "Not included (no ",
                      "Regenerate summary", "Regeneration requested.", "Already requested at ",
                      "Request failed (HTTP ", "Request failed (network error).",
                      "script-map-author", "Your name", "Your name (other)", "Other…",
                      "Guidance (optional)", "Send", "Cancel", "Guidance used: ",
                      "History (", "first summary", "updated after code change",
                      "regenerated", "copied from identical script",
                      "Nightly runs and Refresh data only summarize new or changed scripts",
                      "Last change"):
            self.assertIn(label, text)
        self.assertNotIn("Record X-ray", text)
        self.assertNotIn("Constellation", text)
        self.assertNotIn("All repos", text)

    def test_inline_script_passes_node_check(self):
        node = shutil.which("node")
        if node is None:
            self.fail("node is required for the inline script syntax check but was not found")
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 0, err)
        page = self.out.read_text(encoding="utf-8")
        bodies = [m.group(2) for m in SCRIPT_TAG_RE.finditer(page)
                  if "type=" not in (m.group(1) or "")]
        self.assertTrue(bodies)
        for i, body in enumerate(bodies):
            js = self.root / f"inline_{i}.js"
            js.write_text(body, encoding="utf-8")
            result = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def run_header_in_node(self, argv, tail="report();", prelude="", repos=None):
        """Render a page, run its inline script in node against a tiny DOM shim and
        return a description of the resulting header (or whatever tail prints).
        `prelude` runs after the shim and before the page script; `repos` maps a label
        to {relative path: source} to render instead of the default one-script repo."""
        node = shutil.which("node")
        if node is None:
            self.fail("node is required for the header behavior check but was not found")
        if repos is None:
            repo = self.make_repo("a", "ref: refs/heads/sandbox\n")
            self.write("a/ue.js", USER_EVENT)
            repo_args = ["--repo", f"RTMH={repo}"]
        else:
            repo_args = []
            for label, files in repos.items():
                repo = self.make_repo(label.lower(), "ref: refs/heads/sandbox\n")
                for rel, source in files.items():
                    self.write(f"{label.lower()}/{rel}", source)
                repo_args += ["--repo", f"{label}={repo}"]
        code, _, err = self.run_main(repo_args + ["--out", str(self.out),
                                      "--branch", "sandbox", "--title", "Map"] + list(argv))
        self.assertEqual(code, 0, err)
        page = self.out.read_text(encoding="utf-8")
        payload = DATA_RE.search(page).group(1)
        body = [m.group(2) for m in SCRIPT_TAG_RE.finditer(page)
                if "type=" not in (m.group(1) or "")][0]
        js = self.root / "header_check.js"
        js.write_text(DOM_SHIM + "\nvar DATA_TEXT = " + json.dumps(payload) + ";\n"
                      + prelude + "\n"
                      + "setup();\n" + body + "\n" + tail + "\n", encoding="utf-8")
        result = subprocess.run([node, str(js)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_header_production_badge_and_toggle(self):
        header = self.run_header_in_node(["--switch-url", "/sandbox.html",
                                          "--switch-label", "Sandbox"])
        self.assertEqual(header["document_title"], "Map")
        self.assertEqual(header["order"][:3], ["h1", "span.env-badge", "span.env-toggle"])
        self.assertEqual(header["badge"], {"class": "env-badge", "text": "PRODUCTION"})
        self.assertEqual(header["toggle"], [
            {"tag": "span", "class": "active", "text": "Production", "href": None,
             "aria-current": "page"},
            {"tag": "a", "class": "", "text": "Sandbox", "href": "/sandbox.html",
             "aria-current": None},
        ])
        self.assertEqual(header["chips"], ["RTMH · 1 scripts · sandbox"])
        self.assertEqual(header["chip_classes"], ["chip"])
        self.assertIsNone(header["missing"])

    def test_header_sandbox_badge_toggle_and_missing_line(self):
        header = self.run_header_in_node([
            "--environment", "Sandbox", "--switch-url", "/", "--switch-label", "Production",
            "--missing-repo", "WCS", "--missing-repo", "ANS"])
        self.assertEqual(header["document_title"], "Map (SANDBOX)")
        self.assertEqual(header["badge"], {"class": "env-badge warn", "text": "SANDBOX"})
        self.assertEqual(header["toggle"], [
            {"tag": "a", "class": "", "text": "Production", "href": "/",
             "aria-current": None},
            {"tag": "span", "class": "active", "text": "Sandbox", "href": None,
             "aria-current": "page"},
        ])
        self.assertEqual(header["missing"], "Not included (no sandbox branch): WCS, ANS")

    def test_header_without_switch_url_has_no_toggle(self):
        header = self.run_header_in_node(["--environment", "Sandbox"])
        self.assertEqual(header["badge"], {"class": "env-badge warn", "text": "SANDBOX"})
        self.assertIsNone(header["toggle"])
        self.assertNotIn("span.env-toggle", header["order"])

    def run_regenerate_in_node(self, argv, replies):
        """Click the first Regenerate summary button once per reply and return what the
        fake fetch received and the status text after each click."""
        return self.run_header_in_node(
            argv, "regenerate(" + json.dumps(replies) + ");")

    def test_regenerate_button_absent_without_refresh_url(self):
        result = self.run_regenerate_in_node(["--summary-namespace", "sandbox"], [])
        self.assertEqual(result["entries"], 1)
        self.assertEqual(result["buttons"], 0)

    def test_regenerate_button_posts_summary_key(self):
        result = self.run_regenerate_in_node(
            ["--refresh-url", "/api/rebuild", "--summary-namespace", "sandbox"],
            [{"status": 202}])
        self.assertEqual(result["entries"], 1)
        self.assertEqual(result["buttons"], 1)
        self.assertEqual(result["button_text"], "Regenerate summary")
        self.assertEqual(result["status_role"], "status")
        self.assertFalse(result["in_summary_row"])
        call = result["calls"][0]
        self.assertEqual(call["url"], "/api/rebuild")
        self.assertEqual(call["init"]["method"], "POST")
        self.assertEqual(call["init"]["headers"], {"content-type": "application/json"})
        self.assertEqual(json.loads(call["init"]["body"]),
                         {"key": "sandbox|RTMH:ue.js", "author": "Kim", "guidance": ""})
        self.assertEqual(result["clicks"], [{
            "disabled_during": True,
            "status": "Regeneration requested. New summary in about 10 minutes, "
                      "then reload this page.",
            "disabled": True}])

    def test_regenerate_button_error_outcomes(self):
        result = self.run_regenerate_in_node(["--refresh-url", "/api/rebuild"], [
            {"status": 429, "json": {"requested_at": "2026-10-12T09:58:00Z"}},
            {"status": 500},
            {"status": 429, "bad_json": True},
            {"status": 429, "json": {}},
            {"throw": True},
        ])
        self.assertEqual(json.loads(result["calls"][0]["init"]["body"]),
                         {"key": "RTMH:ue.js", "author": "Kim", "guidance": ""})
        self.assertEqual(result["clicks"], [
            {"disabled_during": True, "disabled": False,
             "status": "Already requested at 09:58 UTC. Try again after 10:08 UTC."},
            {"disabled_during": True, "disabled": False, "status": "Request failed (HTTP 500)."},
            {"disabled_during": True, "disabled": False,
             "status": "Request failed (network error)."},
            {"disabled_during": True, "disabled": False,
             "status": "Request failed (network error)."},
            {"disabled_during": True, "disabled": False,
             "status": "Request failed (network error)."},
        ])

    # Summary generated time and pending request status

    def test_ai_summary_generated_suffix(self):
        repos = {"RTMH": {n: USER_EVENT for n in ("a.js", "b.js", "c.js", "d.js", "e.js")}}
        values = {"a.js": ["ai", "2026-03-04T05:06:07Z"], "b.js": ["ai", ""],
                  "c.js": ["ai", "not a date"], "d.js": ["header", "2026-03-04T05:06:07Z"],
                  "e.js": ["ai", "2026-03-04T05:06:07.123+00:00"]}
        prelude = ("editData(function (d) { var v = %s; d.nodes.forEach(function (n) {"
                   " if (v[n.name]) { n.summary = 'Text.'; n.summary_source = v[n.name][0];"
                   " n.summary_generated_at = v[n.name][1]; } }); });" % json.dumps(values))
        labels = self.run_header_in_node(
            [], "console.log(JSON.stringify(summaryLabels()));", prelude, repos)
        self.assertEqual(labels, {
            "a.js": [" · AI summary · generated 2026-03-04 05:06 UTC"],
            "b.js": [" · AI summary"],
            "c.js": [" · AI summary"],
            "d.js": [" · From header comment"],
            "e.js": [" · AI summary · generated 2026-03-04 05:06 UTC"],
        })

    def test_ai_summary_generated_suffix_via_cache(self):
        sources = {"ai.js": USER_EVENT, "hdr.js": USER_EVENT.replace("/**\n", "/**\n * Hdr.\n")}
        sha = hashlib.sha256(USER_EVENT.encode("utf-8")).hexdigest()
        cache = self.write_cache({"RTMH:ai.js": dict(self.summary_entry(sha, "Holds."),
                                                     generated_at="2026-01-02T03:04:05Z")})
        labels = self.run_header_in_node(
            ["--summaries", str(cache)], "console.log(JSON.stringify(summaryLabels()));",
            "", {"RTMH": sources})
        self.assertEqual(labels, {"ai.js": [" · AI summary · generated 2026-01-02 03:04 UTC"],
                                  "hdr.js": [" · From header comment"]})

    STATUS_TAIL = "afterTicks(function () { console.log(JSON.stringify(statusReport())); });"

    def run_status_in_node(self, reply, argv=("--refresh-url", "/api/rebuild"),
                           generated_at="2026-10-12T10:00:00Z", tail=STATUS_TAIL):
        prelude = "enableQuerySelector();\n"
        if generated_at is None:
            prelude += "editData(function (d) { delete d.generated_at; });\n"
        else:
            prelude += "editData(function (d) { d.generated_at = %s; });\n" % json.dumps(
                generated_at)
        if reply is not None:
            prelude += "statusFetch(%s);\n" % json.dumps(reply)
        return self.run_header_in_node(list(argv), tail, prelude, SEARCH_REPOS)

    def test_status_get_sent_once_with_refresh_url(self):
        out = self.run_status_in_node({"status": 200, "json": {
            "status": "ok", "rebuild_requested_at": None, "requests": {}}})
        self.assertEqual(out["calls"], [
            {"url": "/api/rebuild", "init": {"method": "GET", "cache": "no-store"}}])
        self.assertEqual(out["refresh_status"], "")
        self.assertEqual({v["status"] for v in out["inventory"].values()}, {""})

    def test_status_get_not_sent_without_refresh_url(self):
        out = self.run_status_in_node({"status": 200, "json": {}}, argv=())
        self.assertEqual(out["calls"], [])
        self.assertIsNone(out["refresh_status"])
        self.assertEqual(set(out["inventory"].values()), {None})

    def test_status_refresh_pending_message(self):
        cases = {
            "2026-10-12T10:07:00Z": "Refresh requested at 10:07 UTC. New data usually within "
                                    "10 minutes; reload to check.",
            "2026-10-12T10:00:00Z": "",
            "2026-10-12T09:59:59Z": "",
            None: "",
            "not a date": "",
            5: "",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                out = self.run_status_in_node({"status": 200, "json": {
                    "status": "ok", "rebuild_requested_at": value, "requests": {}}})
                self.assertEqual(out["refresh_status"], expected)

    def test_status_regenerate_pending_in_inventory_and_records(self):
        later = "2026-10-12T10:05:00Z"
        reply = {"status": 200, "json": {"status": "ok", "rebuild_requested_at": None,
                 "requests": {
                     "RTMH:Req.Dep.js": later,
                     "WCS:hold.js": "2026-10-12T10:00:00Z",
                     "RTMH:req_other.js": "2026-10-12T09:00:00Z",
                     "sandbox|WCS:hold.js": later,
                     "X:unknown.js": later,
                 }}}
        tail = """
afterTicks(function () {
    var out = {};
    out.load = statusReport();
    click(areaCard('Custom records'));
    out.records = regenStatuses('rec-list');
    typeSearch('req');
    out.searched = regenStatuses('i-list');
    out.searched_records = regenStatuses('rec-list');
    typeSearch('');
    out.cleared = regenStatuses('i-list');
    console.log(JSON.stringify(out));
});
"""
        out = self.run_status_in_node(reply, tail=tail)
        pending = ("Regeneration requested at 10:05 UTC. New summary usually within "
                   "10 minutes; reload to check.")
        idle = {"status": "", "disabled": False, "status_is_second": True}
        busy = {"status": pending, "disabled": False, "status_is_second": True}
        self.assertEqual(out["load"]["refresh_status"], "")
        self.assertEqual(out["load"]["inventory"],
                         {"Req.Dep.js": busy, "hold.js": idle, "req_other.js": idle})
        self.assertEqual(out["records"], {"Req.Dep.js": busy, "hold.js": idle})
        self.assertEqual(out["searched"], {"Req.Dep.js": busy, "req_other.js": idle})
        self.assertEqual(out["searched_records"], {"Req.Dep.js": busy})
        self.assertEqual(out["cleared"],
                         {"Req.Dep.js": busy, "hold.js": idle, "req_other.js": idle})

    def test_status_regenerate_pending_with_namespace(self):
        later = "2026-10-12T10:05:00Z"
        reply = {"status": 200, "json": {"status": "ok", "rebuild_requested_at": None,
                 "requests": {"RTMH:Req.Dep.js": later, "sandbox|WCS:hold.js": later}}}
        out = self.run_status_in_node(
            reply, argv=("--refresh-url", "/api/rebuild", "--summary-namespace", "sandbox"))
        self.assertEqual({k: v["status"] != "" for k, v in out["inventory"].items()},
                         {"Req.Dep.js": False, "hold.js": True, "req_other.js": False})

    def test_status_unavailable_on_failure(self):
        replies = {
            "default throwing fetch": None,
            "sync throw": {"sync_throw": True},
            "rejected": {"reject": True},
            "http 502": {"status": 502, "json": {"status": "error"}},
            "http 404": {"status": 404, "json": {}},
            "bad json": {"status": 200, "bad_json": True},
            "null body": {"status": 200, "json": None},
            "array body": {"status": 200, "json": [1]},
            "string body": {"status": 200, "json": "ok"},
        }
        for name, reply in replies.items():
            with self.subTest(name=name):
                out = self.run_status_in_node(reply)
                self.assertEqual(out["refresh_status"], "Status unavailable.")
                self.assertEqual({v["status"] for v in out["inventory"].values()}, {""})
                self.assertEqual({v["disabled"] for v in out["inventory"].values()}, {False})
                if reply is not None:
                    self.assertEqual(len(out["calls"]), 1)

    def test_status_unparseable_generated_at_means_nothing_pending(self):
        later = "2999-01-01T00:00:00Z"
        reply = {"status": 200, "json": {"status": "ok", "rebuild_requested_at": later,
                 "requests": {"RTMH:Req.Dep.js": later}}}
        for generated_at in ("not a date", "", None):
            with self.subTest(generated_at=generated_at):
                out = self.run_status_in_node(reply, generated_at=generated_at)
                self.assertEqual(len(out["calls"]), 1)
                self.assertEqual(out["refresh_status"], "")
                self.assertEqual({v["status"] for v in out["inventory"].values()}, {""})

    def test_status_does_not_overwrite_click_text(self):
        # The Regenerate click lands before the status reply is applied; its text stays.
        prelude_reply = {"status": 200, "json": {"status": "ok", "rebuild_requested_at": None,
                         "requests": {"RTMH:Req.Dep.js": "2026-10-12T10:05:00Z"}}}
        tail = """
var span = null;
walk(byIdMap['i-list'], function (c) { return c.tagName === 'details'; }).forEach(function (d) {
    if (entryName(d) === 'Req.Dep.js') {
        span = walk(d, function (c) { return c.className === 'regen-status'; })[0];
    }
});
span.textContent = 'Regeneration requested. New summary in about 10 minutes, then reload this page.';
afterTicks(function () { console.log(JSON.stringify(statusReport())); });
"""
        out = self.run_status_in_node(prelude_reply, tail=tail)
        self.assertEqual(out["inventory"]["Req.Dep.js"]["status"],
                         "Regeneration requested. New summary in about 10 minutes, "
                         "then reload this page.")

    # Theme toggle and script search

    def css_rule(self, text, selector):
        match = re.search(r"(?m)^" + re.escape(selector) + r" \{([^}]*)\}", text)
        self.assertIsNotNone(match, selector)
        decls = {}
        for part in match.group(1).split(";"):
            if part.strip():
                key, value = part.split(":", 1)
                decls[key.strip()] = value.strip()
        return decls

    def test_template_theme_palettes(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn("prefers-color-scheme", text)
        self.assertEqual(self.css_rule(text, ":root"), DARK_THEME)
        self.assertEqual(self.css_rule(text, ':root[data-theme="light"]'), LIGHT_THEME)
        self.assertEqual(text.count(":root {"), 1)
        self.assertEqual(text.count(':root[data-theme="light"] {'), 1)

    def test_template_accent_contrast_on_active_rules(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        for selector in (".env-toggle .active", "nav button.active", "#rec-chips button.active"):
            with self.subTest(selector=selector):
                rule = self.css_rule(text, selector)
                self.assertEqual(rule["background"], "var(--accent)")
                self.assertEqual(rule["color"], "var(--accent-contrast)")
        self.assertNotRegex(text, r"color:\s*#fff\b")

    def test_template_regenerate_button_style(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        rule = self.css_rule(text, ".regen button")
        self.assertEqual(rule["background"], "var(--accent)")
        self.assertEqual(rule["border-color"], "var(--accent)")
        self.assertEqual(rule["color"], "var(--accent-contrast)")
        self.assertEqual(rule["font-weight"], "600")
        self.assertEqual(self.css_rule(text, ".regen button:disabled"),
                         {"cursor": "default", "opacity": "0.6"})

    def test_template_search_markup(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        search = ('<input type="search" id="script-search" list="script-search-options" '
                  'placeholder="Search scripts" aria-label="Search scripts" autocomplete="off">')
        datalist = '<datalist id="script-search-options"></datalist>'
        self.assertEqual(text.count(search), 1)
        self.assertEqual(text.count(datalist), 1)
        controls = text.index('<div class="controls" id="i-controls">')
        repo = text.index('<select id="i-repo"')
        self.assertLess(controls, text.index(search))
        self.assertLess(text.index(search), text.index(datalist))
        self.assertLess(text.index(datalist), repo)
        # Nothing but whitespace between the row's opening tag, the input, the datalist
        # and #i-repo: they are the row's first children in that order.
        between = text[controls + len('<div class="controls" id="i-controls">'):repo]
        self.assertEqual(between.split(), search.split() + datalist.split())
        self.assertIn("#script-search { width: 100%; }", text)

    def test_rendered_page_has_two_script_tags(self):
        repo = self.make_repo("a")
        self.write("a/ue.js", USER_EVENT)
        code, _, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out),
                                      "--refresh-url", "/api/rebuild"])
        self.assertEqual(code, 0, err)
        page = self.out.read_text(encoding="utf-8")
        self.assertEqual(page.count("<script"), 2)
        self.assertEqual(len(SCRIPT_TAG_RE.findall(page)), 2)
        self.assertNotIn("<link", page)

    def test_theme_defaults_to_dark_without_window(self):
        states = self.run_header_in_node([], THEME_TAIL)
        self.assertEqual(states[0], {
            "tag": "button", "type": "button", "text": "Light mode",
            "aria": "Switch to light mode", "last": True, "data_theme": None, "stored": None})
        # No document.documentElement: the button still toggles.
        self.assertEqual(states[1]["text"], "Dark mode")
        self.assertEqual(states[1]["aria"], "Switch to dark mode")
        self.assertEqual(states[2]["text"], "Light mode")

    def test_theme_toggle_is_last_after_env_toggle_and_refresh(self):
        header = self.run_header_in_node(
            ["--switch-url", "/sandbox.html", "--switch-label", "Sandbox",
             "--refresh-url", "/api/rebuild"])
        self.assertEqual(header["order"][:3], ["h1", "span.env-badge", "span.env-toggle"])
        self.assertEqual(header["order"][-1], "button")
        self.assertEqual(header["order"].count("button"), 1)

    def test_theme_click_writes_storage_and_sets_data_theme(self):
        states = self.run_header_in_node([], THEME_TAIL, STORAGE_PRELUDE % "{}")
        self.assertEqual(states, [
            {"tag": "button", "type": "button", "text": "Light mode",
             "aria": "Switch to light mode", "last": True, "data_theme": "dark", "stored": None},
            {"tag": "button", "type": "button", "text": "Dark mode",
             "aria": "Switch to dark mode", "last": True, "data_theme": "light",
             "stored": "light"},
            {"tag": "button", "type": "button", "text": "Light mode",
             "aria": "Switch to light mode", "last": True, "data_theme": "dark",
             "stored": "dark"},
        ])

    def test_theme_stored_values(self):
        cases = {"light": "light", "LIGHT": "dark", " light": "dark", "1": "dark",
                 "": "dark", "dark": "dark"}
        for stored, expected in cases.items():
            with self.subTest(stored=stored):
                prelude = STORAGE_PRELUDE % json.dumps({"script-map-theme": stored})
                states = self.run_header_in_node([], THEME_TAIL, prelude)
                self.assertEqual(states[0]["data_theme"], expected)
                self.assertEqual(states[0]["text"],
                                 "Dark mode" if expected == "light" else "Light mode")

    def test_theme_throwing_storage_loads_dark_and_still_toggles(self):
        states = self.run_header_in_node([], THEME_TAIL, THROWING_STORAGE_PRELUDE)
        self.assertEqual([(s["text"], s["data_theme"]) for s in states], [
            ("Light mode", "dark"), ("Dark mode", "light"), ("Light mode", "dark")])

    def run_search_in_node(self, tail):
        return self.run_header_in_node([], tail, "enableQuerySelector();", SEARCH_REPOS)

    def test_search_datalist_options(self):
        snap = self.run_search_in_node("console.log(JSON.stringify(snapshot()));")
        values = [o["value"] for o in snap["options"]]
        self.assertEqual(sorted(values), ["Req.Dep.js", "hold.js", "req_other.js"])
        self.assertEqual({o["tag"] for o in snap["options"]}, {"option"})
        expected = subprocess.run(
            [shutil.which("node"), "-e",
             "console.log(JSON.stringify(%s.sort(function (a, b) { return a.localeCompare(b); })))"
             % json.dumps(["req_other.js", "Req.Dep.js", "hold.js"])],
            capture_output=True, text=True)
        self.assertEqual(values, json.loads(expected.stdout))

    def test_search_datalist_dedups_names_across_repos(self):
        repos = {"RTMH": {"ue.js": USER_EVENT}, "WCS": {"ue.js": USER_EVENT}}
        snap = self.run_header_in_node([], "console.log(JSON.stringify(snapshot()));",
                                       "enableQuerySelector();", repos)
        self.assertEqual([o["value"] for o in snap["options"]], ["ue.js"])
        self.assertEqual(snap["list_heading"], "2 scripts")

    def test_search_filters_inventory_and_risk(self):
        tail = """
var out = {};
out.none = snapshot();
['REQ', '  req ', 'req.', 'zzz', '   ', 'hold'].forEach(function (q) {
    typeSearch(q);
    out[q] = snapshot();
});
console.log(JSON.stringify(out));
"""
        out = self.run_search_in_node(tail)
        everyone = ["Req.Dep.js", "hold.js", "req_other.js"]
        self.assertEqual(sorted(out["none"]["list"]), everyone)
        self.assertEqual(out["none"]["list_heading"], "3 scripts")
        self.assertEqual(sorted(out["none"]["risk"]), everyone)
        for q in ("REQ", "  req "):
            with self.subTest(q=q):
                self.assertEqual(sorted(out[q]["list"]), ["Req.Dep.js", "req_other.js"])
                self.assertEqual(out[q]["list_heading"], "2 scripts")
                self.assertEqual(sorted(out[q]["risk"]), ["Req.Dep.js", "req_other.js"])
        # A literal "." (no RegExp): "req." matches Req.Dep.js but not req_other.js.
        self.assertEqual(out["req."]["list"], ["Req.Dep.js"])
        self.assertEqual(out["req."]["risk"], ["Req.Dep.js"])
        self.assertEqual(out["zzz"]["list"], [])
        self.assertEqual(out["zzz"]["list_heading"], "0 scripts")
        self.assertEqual(out["zzz"]["list_empty"], ["No scripts found"])
        self.assertEqual(out["zzz"]["risk"], [])
        self.assertEqual(out["zzz"]["risk_empty"], ["No scripts found"])
        self.assertEqual(sorted(out["   "]["list"]), everyone)
        self.assertEqual(sorted(out["   "]["risk"]), everyone)
        self.assertEqual(out["hold"]["list"], ["hold.js"])
        # Tiles and the matrix follow the shared search like every other filter (AC 5).
        self.assertIn("Total scripts3", out["none"]["tiles"])
        for q, total in (("REQ", 2), ("req.", 1), ("zzz", 0), ("hold", 1)):
            with self.subTest(q=q):
                self.assertTrue(out[q]["tiles"].startswith("Total scripts%d" % total))
                self.assertEqual(out[q]["search"], q)
        self.assertEqual(out["   "]["tiles"], out["none"]["tiles"])
        self.assertEqual(out["   "]["matrix"], out["none"]["matrix"])
        self.assertEqual(out["zzz"]["matrix"], "No scripts found")

    def test_search_does_not_match_path_or_summary(self):
        repos = {"RTMH": {"Sub/Req/ue.js": USER_EVENT}}
        out = self.run_header_in_node(
            [], "typeSearch('req'); console.log(JSON.stringify(snapshot()));",
            "enableQuerySelector();", repos)
        self.assertEqual(out["list"], [])
        self.assertEqual(out["risk_empty"], ["No scripts found"])

    def test_search_combines_with_filters_and_clear_resets(self):
        tail = """
var out = {};
var iType = byIdMap['i-type'];
iType.value = 'ClientScript';
iType.listeners.change.forEach(function (fn) { fn({}); });
out.type_only = snapshot();
typeSearch('dep');
out.type_and_search = snapshot();
click(byIdMap['i-clear']);
out.cleared = snapshot();
out.cleared_type = iType.value;
console.log(JSON.stringify(out));
"""
        out = self.run_search_in_node(tail)
        self.assertEqual(out["type_only"]["list"], ["req_other.js"])
        self.assertEqual(out["type_and_search"]["list"], [])
        # The Risk grid applies the type filter too now (AC 7).
        self.assertEqual(out["type_only"]["risk"], ["req_other.js"])
        self.assertEqual(out["type_and_search"]["risk"], [])
        self.assertEqual(out["cleared"]["search"], "")
        self.assertEqual(out["cleared_type"], "")
        self.assertEqual(sorted(out["cleared"]["list"]), ["Req.Dep.js", "hold.js", "req_other.js"])
        self.assertEqual(sorted(out["cleared"]["risk"]), ["Req.Dep.js", "hold.js", "req_other.js"])
        self.assertEqual(len(out["cleared"]["areas"]), 4)

    def test_search_filters_records(self):
        tail = """
var out = {};
function areaCard(name) {
    return byIdMap['rec-cards'].children.filter(function (c) {
        return c.children[0].textContent === name;
    })[0];
}
function chip(prefix) {
    return byIdMap['rec-chips'].children.filter(function (c) {
        return c.textContent.indexOf(prefix) === 0;
    })[0];
}
out.none = snapshot();
click(areaCard('Custom records'));
out.none_area = snapshot();
click(chip('Sales Order Hold'));
out.none_record = snapshot();
typeSearch('req');
out.req_record = snapshot();
click(chip('All Custom records'));
out.req_area = snapshot();
typeSearch('hold');
out.hold = snapshot();
typeSearch('zzz');
out.zzz = snapshot();
console.log(JSON.stringify(out));
"""
        out = self.run_search_in_node(tail)
        self.assertEqual(out["none"]["areas"], [
            ["Sales", "1 scripts", "1 record types"],
            ["Billing / AR", "1 scripts", "1 record types"],
            ["Entities", "1 scripts", "1 record types"],
            ["Custom records", "2 scripts", "1 record types"],
        ])
        self.assertEqual(out["none"]["missing"], ["RTMH: req_other.js"])
        self.assertTrue(out["none"]["missing_count"].startswith("1 "))
        self.assertEqual(out["none_area"]["rec_chips"], [
            "All Custom records", "Sales Order Hold [customrecord2326] · 2"])
        self.assertEqual(sorted(out["none_area"]["rec_list"]), ["Req.Dep.js", "hold.js"])
        self.assertEqual(out["none_record"]["lanes"], [["beforeLoad", "Req.Dep.js", "hold.js"]])
        # With a query the selected record keeps only the matching script.
        self.assertEqual(out["req_record"]["areas"], [
            ["Sales", "1 scripts", "1 record types"],
            ["Entities", "1 scripts", "1 record types"],
            ["Custom records", "1 scripts", "1 record types"],
        ])
        self.assertEqual(out["req_record"]["rec_chips"], [
            "All Custom records", "Sales Order Hold [customrecord2326] · 1"])
        self.assertEqual(out["req_record"]["lanes"], [["beforeLoad", "Req.Dep.js"]])
        self.assertEqual(out["req_record"]["missing"], ["RTMH: req_other.js"])
        self.assertEqual(out["req_area"]["rec_list_heading"], "1 scripts")
        self.assertEqual(out["req_area"]["rec_list"], ["Req.Dep.js"])
        self.assertEqual(out["hold"]["areas"], [
            ["Billing / AR", "1 scripts", "1 record types"],
            ["Custom records", "1 scripts", "1 record types"],
        ])
        self.assertEqual(out["hold"]["rec_list"], ["hold.js"])
        self.assertEqual(out["hold"]["missing"], [])
        self.assertTrue(out["hold"]["missing_count"].startswith("0 "))
        self.assertEqual(out["zzz"]["areas"], [])
        self.assertEqual(out["zzz"]["rec_chips"], ["No scripts found"])

    def test_search_with_zero_scripts(self):
        repos = {"RTMH": {"README.txt": "nothing here"}}
        out = self.run_header_in_node(
            [], "typeSearch('x'); console.log(JSON.stringify(snapshot()));",
            "enableQuerySelector();", repos)
        self.assertEqual(out["options"], [])
        self.assertEqual(out["list_empty"], ["No scripts found"])
        self.assertEqual(out["risk_empty"], ["No scripts found"])
        self.assertEqual(out["rec_chips"], ["No scripts found"])

    # Search placement, Regenerate dialog, summary how/who and history

    def test_template_controls_rows_have_ids(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        opening = '<div class="controls" id="i-controls">'
        self.assertEqual(text.count(opening), 1)
        # One shared bar: the first child of <main>, before every view section.
        main = text[text.index("<main>") + len("<main>"):]
        self.assertTrue(main.lstrip().startswith(opening))
        self.assertLess(text.index(opening), text.index('<section id="view-inventory"'))
        self.assertLess(text.index(opening), text.index('<select id="i-repo"'))
        for gone in ("rec-controls", "r-controls", "rec-repo", "r-repo"):
            self.assertNotIn(gone, text)
        self.assertEqual(text.count('class="controls"'), 1)
        self.assertEqual(text.count('<div class="controls">'), 0)
        header = text[text.index("<header>"):text.index("</header>")]
        self.assertNotIn("script-search", header)
        self.assertEqual(text.count("<script"), 2)

    def test_search_moves_with_views_and_keeps_text(self):
        prelude = """
enableQuerySelector();
enableNav();
['script-search', 'script-search-options', 'i-repo'].forEach(function (id) {
    document.getElementById('i-controls').appendChild(document.getElementById(id));
});
"""
        tail = """
var input = byIdMap['script-search'];
var options = byIdMap['script-search-options'];
function state() {
    var s = snapshot();
    return {
        rows: ['i-controls'].map(function (id) {
            return byIdMap[id].children.map(function (c) { return c.id; });
        }),
        hidden: ['view-inventory', 'view-records', 'view-risk'].map(function (id) {
            return byIdMap[id].hidden;
        }),
        same: byIdMap['script-search'] === input && byIdMap['script-search-options'] === options,
        listeners: input.listeners.input.length,
        options: options.children.length,
        value: input.value,
        list: s.list.sort(),
        risk: s.risk.sort(),
        areas: s.areas.length
    };
}
var out = {};
out.start = state();
typeSearch('req');
navTo('records');
out.records = state();
navTo('risk');
out.risk = state();
typeSearch('hold');
out.risk_hold = state();
navTo('inventory');
out.inventory = state();
console.log(JSON.stringify(out));
"""
        out = self.run_header_in_node([], tail, prelude, SEARCH_REPOS)
        search = ["script-search", "script-search-options"]
        # The search box stays in the one shared bar in every view.
        self.assertEqual(out["start"]["rows"], [search + ["i-repo"]])
        self.assertEqual(out["start"]["hidden"], [False, True, True])
        self.assertEqual(out["records"]["rows"], [search + ["i-repo"]])
        self.assertEqual(out["records"]["hidden"], [True, False, True])
        self.assertEqual(out["risk"]["rows"], [search + ["i-repo"]])
        self.assertEqual(out["risk"]["hidden"], [True, True, False])
        self.assertEqual(out["inventory"]["rows"], [search + ["i-repo"]])
        for key in ("start", "records", "risk", "risk_hold", "inventory"):
            with self.subTest(key=key):
                self.assertTrue(out[key]["same"])
                self.assertEqual(out[key]["listeners"], 1)
                self.assertEqual(out[key]["options"], 3)
        # The typed text survives the moves and keeps filtering every view.
        self.assertEqual(out["records"]["value"], "req")
        self.assertEqual(out["risk"]["value"], "req")
        self.assertEqual(out["risk"]["risk"], ["Req.Dep.js", "req_other.js"])
        self.assertEqual(out["risk"]["list"], ["Req.Dep.js", "req_other.js"])
        self.assertEqual(out["risk_hold"]["risk"], ["hold.js"])
        self.assertEqual(out["inventory"]["value"], "hold")
        self.assertEqual(out["inventory"]["list"], ["hold.js"])

    # Global filters, clickable counts, inline Risk detail and URL hash

    def run_filters_in_node(self, tail, hash_=None, argv=()):
        prelude = FILTER_PRELUDE
        if hash_ is not None:
            prelude = HASH_PRELUDE % json.dumps(hash_) + prelude
        return self.run_header_in_node(list(argv), tail, prelude, SEARCH_REPOS)

    def test_template_global_filter_markup_and_css(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        bar = text[text.index('<div class="controls" id="i-controls">'):]
        bar = bar[:bar.index("</div>")]
        ids = re.findall(r'id="([^"]+)"', bar)
        self.assertEqual(ids, ["i-controls", "script-search", "script-search-options", "i-repo",
                               "i-type", "i-version", "i-risk", "i-clear"])
        self.assertIn('<select id="i-risk" aria-label="Risk filter">', bar)
        self.assertEqual(re.findall(r'<option value="([^"]*)">([^<]*)</option>', bar),
                         [("", "All risk"), ("high", "High"), ("medium", "Medium"),
                          ("low", "Low")])
        self.assertIn('<button type="button" id="i-clear">Clear all</button>', bar)
        self.assertNotIn("Clear filters", text)
        risk = text[text.index('<section id="view-risk"'):]
        self.assertTrue(risk[risk.index(">") + 1:].lstrip().startswith('<div id="r-tiles"></div>'))
        self.assertLess(risk.index('id="r-tiles"'), risk.index('id="r-grid"'))
        self.assertEqual(self.css_rule(text, "button.tile"), {
            "font": "inherit", "color": "inherit", "text-align": "left", "cursor": "pointer"})
        self.assertEqual(self.css_rule(text, ".tile.sel"), {"outline": "2px solid var(--accent)"})
        self.assertEqual(self.css_rule(text, ".tile.low"), {
            "background": "var(--low-bg)", "border-left-color": "var(--low-border)"})
        self.assertEqual(self.css_rule(text, ".r-detail"), {"grid-column": "1 / -1"})
        self.assertEqual(self.css_rule(text, "#repo-chips button.chip.active"), {
            "background": "var(--accent)", "border-color": "var(--accent)",
            "color": "var(--accent-contrast)"})
        self.assertEqual(self.css_rule(text, ".controls")["flex-wrap"], "wrap")
        media = text[text.index("@media (max-width: 600px) {"):]
        media = media[:media.index("\n}\n")]
        self.assertIn(".controls { flex-direction: column; align-items: stretch; }", media)
        for name in ("rec-repo", "r-repo", "openInInventory", "controlRows"):
            self.assertNotIn(name, text)

    def test_global_filters_apply_to_all_views(self):
        tail = """
var out = {};
out.start = view();
click(chipByRepo('RTMH'));
change('i-risk', 'medium');
out.rtmh_medium = view();
out.rtmh_medium_records = snapshot().areas;
change('i-risk', 'low');
out.rtmh_low = view();
navTo('records');
out.records_view = view();
console.log(JSON.stringify(out));
"""
        out = self.run_filters_in_node(tail)
        everyone = ["Req.Dep.js", "hold.js", "req_other.js"]
        self.assertEqual(out["start"]["list"], everyone)
        self.assertEqual(out["start"]["risk"], everyone)
        self.assertEqual(len(out["start"]["areas"]), 4)
        medium = out["rtmh_medium"]
        self.assertEqual(medium["filters"], {"search": "", "repo": "RTMH", "type": "",
                                             "version": "", "risk": "medium"})
        self.assertEqual(medium["list"], ["req_other.js"])
        self.assertEqual(medium["risk"], ["req_other.js"])
        self.assertEqual(medium["areas"], [["Entities", "1 scripts", "1 record types"]])
        self.assertEqual(medium["missing"], ["RTMH: req_other.js"])
        low = out["rtmh_low"]
        self.assertEqual(low["list"], ["Req.Dep.js"])
        self.assertEqual(low["risk"], ["Req.Dep.js"])
        self.assertEqual(low["areas"], [["Sales", "1 scripts", "1 record types"],
                                        ["Custom records", "1 scripts", "1 record types"]])
        self.assertEqual(low["missing"], [])
        # Switching views keeps the filters.
        self.assertEqual(out["records_view"]["filters"], low["filters"])
        self.assertEqual(out["records_view"]["areas"], low["areas"])
        self.assertEqual(out["records_view"]["shown"], ["view-records"])

    def test_repo_chip_toggles_client(self):
        tail = """
var out = {};
out.start = view();
click(chipByRepo('WCS'));
out.wcs = view();
click(chipByRepo('RTMH'));
out.rtmh = view();
click(chipByRepo('RTMH'));
out.off = view();
console.log(JSON.stringify(out));
"""
        out = self.run_filters_in_node(tail)
        self.assertEqual(out["start"]["chips"], [
            ["RTMH", "button", "chip", "false"], ["WCS", "button", "chip", "false"]])
        self.assertEqual(out["wcs"]["chips"], [
            ["RTMH", "button", "chip", "false"], ["WCS", "button", "chip active", "true"]])
        self.assertEqual(out["wcs"]["filters"]["repo"], "WCS")
        self.assertEqual(out["wcs"]["list"], ["hold.js"])
        self.assertEqual(out["wcs"]["risk"], ["hold.js"])
        self.assertEqual(out["rtmh"]["chips"], [
            ["RTMH", "button", "chip active", "true"], ["WCS", "button", "chip", "false"]])
        self.assertEqual(out["rtmh"]["list"], ["Req.Dep.js", "req_other.js"])
        # A second click on the selected chip clears the client.
        self.assertEqual(out["off"]["chips"], out["start"]["chips"])
        self.assertEqual(out["off"]["filters"]["repo"], "")
        self.assertEqual(out["off"]["list"], ["Req.Dep.js", "hold.js", "req_other.js"])

    def test_version_tile_toggles(self):
        tail = """
var out = {};
out.start = tileState('i-tiles');
click(tileBy('i-tiles', '1.0'));
out.v10 = { tiles: tileState('i-tiles'), view: view() };
click(tileBy('i-tiles', '1.0'));
out.off = { tiles: tileState('i-tiles'), view: view() };
click(tileBy('i-tiles', '2.1'));
out.v21 = view();
click(tileBy('i-tiles', 'Total scripts'));
out.total = { tiles: tileState('i-tiles'), view: view() };
console.log(JSON.stringify(out));
"""
        out = self.run_filters_in_node(tail)
        self.assertEqual(out["start"], [
            ["button", "button", "tile sel", "true", "Total scripts", "3"],
            ["button", "button", "tile high", "false", "1.0", "1"],
            ["button", "button", "tile medium", "false", "2.0", "0"],
            ["button", "button", "tile", "false", "2.x", "0"],
            ["button", "button", "tile", "false", "2.1", "2"],
        ])
        # Tiles ignore the version filter, so the counts stay; the 1.0 tile is selected.
        self.assertEqual(out["v10"]["tiles"], [
            ["button", "button", "tile", "false", "Total scripts", "3"],
            ["button", "button", "tile high sel", "true", "1.0", "1"],
            ["button", "button", "tile medium", "false", "2.0", "0"],
            ["button", "button", "tile", "false", "2.x", "0"],
            ["button", "button", "tile", "false", "2.1", "2"],
        ])
        self.assertEqual(out["v10"]["view"]["filters"]["version"], "1.0")
        self.assertEqual(out["v10"]["view"]["list"], ["hold.js"])
        self.assertEqual(out["v10"]["view"]["risk"], ["hold.js"])
        self.assertEqual(out["off"]["tiles"], out["start"])
        self.assertEqual(out["off"]["view"]["filters"]["version"], "")
        self.assertEqual(out["v21"]["filters"]["version"], "2.1")
        self.assertEqual(out["v21"]["list"], ["Req.Dep.js", "req_other.js"])
        self.assertEqual(out["total"]["tiles"], out["start"])
        self.assertEqual(out["total"]["view"]["filters"]["version"], "")
        self.assertEqual(out["total"]["view"]["list"], ["Req.Dep.js", "hold.js", "req_other.js"])

    def test_risk_tiles_count_and_toggle(self):
        tail = """
var out = {};
out.start = tileState('r-tiles');
click(tileBy('r-tiles', 'High'));
out.high = { tiles: tileState('r-tiles'), view: view() };
click(tileBy('r-tiles', 'High'));
out.off = { tiles: tileState('r-tiles'), view: view() };
click(chipByRepo('RTMH'));
click(tileBy('r-tiles', 'Low'));
out.rtmh_low = { tiles: tileState('r-tiles'), view: view() };
console.log(JSON.stringify(out));
"""
        out = self.run_filters_in_node(tail)
        self.assertEqual(out["start"], [
            ["button", "button", "tile high", "false", "High", "1"],
            ["button", "button", "tile medium", "false", "Medium", "1"],
            ["button", "button", "tile low", "false", "Low", "1"],
        ])
        # The counts ignore the risk filter itself.
        self.assertEqual(out["high"]["tiles"], [
            ["button", "button", "tile high sel", "true", "High", "1"],
            ["button", "button", "tile medium", "false", "Medium", "1"],
            ["button", "button", "tile low", "false", "Low", "1"],
        ])
        self.assertEqual(out["high"]["view"]["filters"]["risk"], "high")
        self.assertEqual(out["high"]["view"]["risk"], ["hold.js"])
        self.assertEqual(out["high"]["view"]["list"], ["hold.js"])
        self.assertEqual(out["off"]["tiles"], out["start"])
        self.assertEqual(out["off"]["view"]["filters"]["risk"], "")
        self.assertEqual(out["off"]["view"]["risk"], ["Req.Dep.js", "hold.js", "req_other.js"])
        # Other filters do apply to the counts.
        self.assertEqual(out["rtmh_low"]["tiles"], [
            ["button", "button", "tile high", "false", "High", "0"],
            ["button", "button", "tile medium", "false", "Medium", "1"],
            ["button", "button", "tile low sel", "true", "Low", "1"],
        ])
        self.assertEqual(out["rtmh_low"]["view"]["risk"], ["Req.Dep.js"])

    def test_risk_card_detail_inline(self):
        tail = """
var out = {};
navTo('risk');
var a = riskCard('req_other.js');
var b = riskCard('hold.js');
out.start = gridState();
click(a);
out.open_a = gridState();
out.open_a_after_card = byIdMap['r-grid'].children[byIdMap['r-grid'].children.indexOf(a) + 1].className;
click(a);
out.closed = gridState();
click(a);
click(b);
out.open_b = gridState();
out.open_b_after_card = byIdMap['r-grid'].children[byIdMap['r-grid'].children.indexOf(b) + 1].className;
out.a_expanded = a.getAttribute('aria-expanded');
var prevented = false;
(b.listeners.keydown || []).forEach(function (fn) {
    fn({ key: 'Enter', preventDefault: function () { prevented = true; } });
});
out.key_closed = gridState();
out.prevented = prevented;
(a.listeners.keydown || []).forEach(function (fn) { fn({ key: ' ', preventDefault: function () {} }); });
out.key_open = gridState();
typeSearch('');
change('i-risk', 'low');
change('i-risk', '');
out.rerendered = gridState();
out.shown = views();
console.log(JSON.stringify(out));
"""
        out = self.run_filters_in_node(tail)
        cards = [c[1] for c in out["start"]]
        self.assertEqual(sorted(cards), ["Req.Dep.js", "hold.js", "req_other.js"])
        self.assertTrue(all(c[0] == "card" and c[2] == "false" for c in out["start"]))
        def detail_after(state, name):
            i = [c[1] if c[0] == "card" else None for c in state].index(name)
            return state[i + 1]
        self.assertEqual(sum(1 for c in out["open_a"] if c[0] == "r-detail"), 1)
        self.assertEqual(detail_after(out["open_a"], "req_other.js"),
                         ["r-detail", 1, "details", True, "req_other.js"])
        self.assertEqual(out["open_a_after_card"], "r-detail")
        self.assertIn(["card", "req_other.js", "true"], out["open_a"])
        self.assertEqual(out["closed"], out["start"])
        # Opening another card moves the single detail.
        self.assertEqual(sum(1 for c in out["open_b"] if c[0] == "r-detail"), 1)
        self.assertEqual(detail_after(out["open_b"], "hold.js"),
                         ["r-detail", 1, "details", True, "hold.js"])
        self.assertEqual(out["open_b_after_card"], "r-detail")
        self.assertEqual(out["a_expanded"], "false")
        self.assertIn(["card", "hold.js", "true"], out["open_b"])
        # Keyboard: Enter on the open card closes it, Space opens another.
        self.assertEqual(out["key_closed"], out["start"])
        self.assertTrue(out["prevented"])
        self.assertEqual(detail_after(out["key_open"], "req_other.js")[0], "r-detail")
        # A re-render closes the detail; the view never switches away from the Risk grid.
        self.assertEqual(out["rerendered"], out["start"])
        self.assertEqual(out["shown"], ["view-risk"])

    def test_clear_all_resets_every_filter(self):
        tail = """
var out = {};
click(chipByRepo('RTMH'));
typeSearch('req');
change('i-type', 'UserEventScript');
click(tileBy('i-tiles', '2.1'));
click(tileBy('r-tiles', 'Low'));
click(areaCard('Sales'));
navTo('risk');
click(riskCard('Req.Dep.js'));
out.before = view();
out.before_grid = gridState();
out.before_area = byIdMap['rec-cards'].children.map(function (c) { return c.className; });
click(byIdMap['i-clear']);
out.after = view();
out.after_grid = gridState();
out.after_area = byIdMap['rec-cards'].children.map(function (c) { return c.className; });
out.after_note = byIdMap['rec-chips'].textContent;
out.text = byIdMap['i-clear'].textContent;
console.log(JSON.stringify(out));
"""
        out = self.run_filters_in_node(tail)
        self.assertEqual(out["before"]["filters"], {"search": "req", "repo": "RTMH",
                                                    "type": "UserEventScript", "version": "2.1",
                                                    "risk": "low"})
        self.assertEqual(out["before"]["list"], ["Req.Dep.js"])
        self.assertIn("card area sel", out["before_area"])
        self.assertTrue(any(c[0] == "r-detail" for c in out["before_grid"]))
        self.assertEqual(out["after"]["filters"], {"search": "", "repo": "", "type": "",
                                                   "version": "", "risk": ""})
        self.assertEqual(out["after"]["chips"], [
            ["RTMH", "button", "chip", "false"], ["WCS", "button", "chip", "false"]])
        self.assertEqual(out["after"]["list"], ["Req.Dep.js", "hold.js", "req_other.js"])
        self.assertEqual(out["after"]["risk"], ["Req.Dep.js", "hold.js", "req_other.js"])
        self.assertEqual(len(out["after"]["areas"]), 4)
        self.assertNotIn("card area sel", out["after_area"])
        self.assertEqual(out["after_note"], "Pick an area to see its record types and scripts.")
        self.assertFalse(any(c[0] == "r-detail" for c in out["after_grid"]))
        self.assertEqual(out["after"]["shown"], ["view-risk"])

    def test_hash_written_on_filter_change(self):
        tail = """
var out = { start: HASH_WRITES.slice() };
click(chipByRepo('WCS'));
out.chip = HASH_WRITES[HASH_WRITES.length - 1];
change('i-risk', 'high');
out.risk = HASH_WRITES[HASH_WRITES.length - 1];
typeSearch('a&b=c d');
out.search = HASH_WRITES[HASH_WRITES.length - 1];
navTo('risk');
out.view = HASH_WRITES[HASH_WRITES.length - 1];
typeSearch('');
click(tileBy('r-tiles', 'High'));
click(chipByRepo('RTMH'));
change('i-type', 'ClientScript');
click(tileBy('i-tiles', '2.1'));
out.all = HASH_WRITES[HASH_WRITES.length - 1];
navTo('inventory');
click(byIdMap['i-clear']);
out.cleared = HASH_WRITES[HASH_WRITES.length - 1];
out.listeners = Object.keys(HASH_LISTENERS);
console.log(JSON.stringify(out));
"""
        out = self.run_filters_in_node(tail, hash_="")
        self.assertEqual(out["start"], [])
        self.assertEqual(out["chip"], "#client=WCS")
        self.assertEqual(out["risk"], "#client=WCS&risk=high")
        self.assertEqual(out["search"], "#client=WCS&risk=high&q=a%26b%3Dc%20d")
        self.assertEqual(out["view"], "#view=risk&client=WCS&risk=high&q=a%26b%3Dc%20d")
        self.assertEqual(out["all"], "#view=risk&client=RTMH&type=ClientScript&version=2.1")
        self.assertEqual(out["cleared"], "")
        self.assertEqual(out["listeners"], ["hashchange"])

    def test_hash_written_without_replace_state(self):
        prelude_hash = "var window = { location: { hash: '' } };\n"
        tail = """
click(chipByRepo('WCS'));
console.log(JSON.stringify(window.location.hash));
"""
        out = self.run_header_in_node([], tail, prelude_hash + FILTER_PRELUDE, SEARCH_REPOS)
        self.assertEqual(out, "#client=WCS")

    def test_hash_read_on_load_and_invalid_ignored(self):
        tail = """
console.log(JSON.stringify({ view: view(), writes: HASH_WRITES.slice() }));
"""
        valid = self.run_filters_in_node(
            tail, hash_="#view=risk&client=WCS&type=UserEventScript&version=1.0&risk=high&q=hol")
        self.assertEqual(valid["view"]["filters"], {
            "search": "hol", "repo": "WCS", "type": "UserEventScript", "version": "1.0",
            "risk": "high"})
        self.assertEqual(valid["view"]["shown"], ["view-risk"])
        self.assertEqual(valid["view"]["risk"], ["hold.js"])
        self.assertEqual(valid["view"]["chips"][1], ["WCS", "button", "chip active", "true"])
        self.assertEqual(valid["writes"], [])

        cases = {
            "invalid client": ("#client=HJL&risk=low",
                               {"search": "", "repo": "", "type": "", "version": "",
                                "risk": "low"}),
            "invalid risk": ("#client=RTMH&risk=extreme",
                             {"search": "", "repo": "RTMH", "type": "", "version": "",
                              "risk": ""}),
            "malformed pairs": ("#novalue&=x&q=%E0%A4%A&bogus=1&type=ClientScript&&",
                                {"search": "", "repo": "", "type": "ClientScript",
                                 "version": "", "risk": ""}),
            "invalid view, type, version and long q": (
                "#view=graph&type=Nope&version=9.9&q=" + "a" * 101,
                {"search": "", "repo": "", "type": "", "version": "", "risk": ""}),
            "encoded q round trip": ("#q=a%26b%3Dc",
                                     {"search": "a&b=c", "repo": "", "type": "", "version": "",
                                      "risk": ""}),
            "100 char q": ("#q=" + "b" * 100,
                           {"search": "b" * 100, "repo": "", "type": "", "version": "",
                            "risk": ""}),
        }
        for name, (hash_, expected) in cases.items():
            with self.subTest(name):
                out = self.run_filters_in_node(tail, hash_=hash_)
                self.assertEqual(out["view"]["filters"], expected)
                self.assertEqual(out["view"]["shown"], ["view-inventory"])

    def test_hashchange_applies_new_hash(self):
        tail = """
var out = {};
window.location.hash = '#view=records&client=RTMH';
HASH_LISTENERS.hashchange.forEach(function (fn) { fn({}); });
out.changed = view();
window.location.hash = '#client=Nope';
HASH_LISTENERS.hashchange.forEach(function (fn) { fn({}); });
out.invalid = view();
console.log(JSON.stringify(out));
"""
        out = self.run_filters_in_node(tail, hash_="#risk=high")
        self.assertEqual(out["changed"]["filters"]["repo"], "RTMH")
        self.assertEqual(out["changed"]["filters"]["risk"], "")
        self.assertEqual(out["changed"]["shown"], ["view-records"])
        self.assertEqual(out["invalid"]["filters"]["repo"], "")
        self.assertEqual(out["invalid"]["shown"], ["view-inventory"])

    def test_switch_link_carries_hash(self):
        tail = """
function href() {
    var toggle = header.children.filter(function (c) { return c.className === 'env-toggle'; })[0];
    return toggle.children.filter(function (c) { return c.tagName === 'a'; })[0].getAttribute('href');
}
var out = { load: href() };
click(chipByRepo('RTMH'));
out.chip = href();
navTo('risk');
out.view = href();
click(byIdMap['i-clear']);
navTo('inventory');
out.cleared = href();
console.log(JSON.stringify(out));
"""
        argv = ["--switch-url", "/sandbox.html", "--switch-label", "Sandbox"]
        out = self.run_filters_in_node(tail, hash_="#client=HJL&risk=low", argv=argv)
        # HJL is not a client on this page, so it is dropped; the rest carries over.
        self.assertEqual(out["load"], "/sandbox.html#risk=low")
        self.assertEqual(out["chip"], "/sandbox.html#client=RTMH&risk=low")
        self.assertEqual(out["view"], "/sandbox.html#view=risk&client=RTMH&risk=low")
        self.assertEqual(out["cleared"], "/sandbox.html")

    def test_renders_without_window(self):
        tail = """
var out = { window: typeof window };
out.start = view();
out.risk_tiles = byIdMap['r-tiles'].children.length;
click(chipByRepo('WCS'));
out.wcs = view();
console.log(JSON.stringify(out));
"""
        out = self.run_filters_in_node(tail)
        self.assertEqual(out["window"], "undefined")
        self.assertEqual(out["start"]["list"], ["Req.Dep.js", "hold.js", "req_other.js"])
        self.assertEqual(out["start"]["risk"], ["Req.Dep.js", "hold.js", "req_other.js"])
        self.assertEqual(out["start"]["shown"], ["view-inventory"])
        self.assertEqual(out["risk_tiles"], 3)
        self.assertEqual(out["wcs"]["list"], ["hold.js"])
        # A window with only localStorage (no location) also renders.
        partial = self.run_header_in_node(
            [], "console.log(JSON.stringify(view()));",
            STORAGE_PRELUDE % "{}" + FILTER_PRELUDE, SEARCH_REPOS)
        self.assertEqual(partial["list"], ["Req.Dep.js", "hold.js", "req_other.js"])

    PANEL_HELP = ("Regenerate runs a fresh summary for this script. Nightly runs and Refresh "
                  "data only summarize new or changed scripts; they never redo existing "
                  "summaries.")

    def run_panel_in_node(self, tail, team=(), prelude="", reply=None, edit=None):
        argv = ["--refresh-url", "/api/rebuild"]
        for name in team:
            argv += ["--team", name]
        if edit:
            prelude += "\neditData(function (d) { %s });" % edit
        tail = "postFetch(%s);\nvar b = regenButton();\n" % json.dumps(
            reply or {"status": 202}) + tail
        return self.run_header_in_node(argv, tail, prelude)

    def test_regenerate_panel_structure_open_toggle_and_cancel(self):
        tail = """
var out = {};
out.closed = panelState(b);
click(b);
out.open = panelState(b);
out.disabled_open = !!b.disabled;
click(b);
out.toggled = panelState(b);
click(b);
out.reopened = panelState(b) !== null;
click(regenPanel(b).children[4].children[1]);
out.cancelled = panelState(b);
out.wrap_after = b.parentNode.children.length;
out.posts = POSTS;
console.log(JSON.stringify(out));
"""
        out = self.run_panel_in_node(tail, team=["Kim", "Alex"])
        self.assertIsNone(out["closed"])
        self.assertEqual(out["open"], {
            "wrap": ["button.", "span.regen-status", "div.regen-panel"],
            "tags": ["select", "input", "textarea", "p", "div"],
            "select_label": "Your name",
            "options": [["Alex", "Alex"], ["Kim", "Kim"], ["__other__", "Other…"]],
            "selected": "Alex",
            "other": {"type": "text", "label": "Your name (other)", "maxlength": "40",
                      "hidden": True, "value": ""},
            "guidance": {"label": "Guidance (optional)", "maxlength": "500",
                         "placeholder": "Optional: what's wrong or what to focus on. "
                                        "Used for this run only."},
            "help": "p:" + self.PANEL_HELP,
            "actions": [["button", "Send"], ["button", "Cancel"]],
            "send_disabled": False,
        })
        self.assertFalse(out["disabled_open"])
        self.assertIsNone(out["toggled"])
        self.assertTrue(out["reopened"])
        self.assertIsNone(out["cancelled"])
        self.assertEqual(out["wrap_after"], 2)
        self.assertEqual(out["posts"], [])

    def test_regenerate_send_disabled_without_name_and_other_input(self):
        tail = """
var out = {};
click(b);
var p = regenPanel(b);
var sel = p.children[0], other = p.children[1], send = p.children[4].children[0];
function st() { return { selected: sel.value, other_hidden: !!other.hidden, send_disabled: !!send.disabled }; }
out.team_default = st();
sel.value = '__other__'; fire(sel, 'change');
out.other_empty = st();
other.value = '   '; fire(other, 'input');
out.other_spaces = st();
click(send);
out.after_disabled_click = { posts: POSTS.length, open: regenPanel(b) !== null };
other.value = ' Lee '; fire(other, 'input');
out.other_named = st();
sel.value = 'Kim'; fire(sel, 'change');
out.team_again = st();
console.log(JSON.stringify(out));
"""
        out = self.run_panel_in_node(tail, team=["Kim"])
        self.assertEqual(out["team_default"],
                         {"selected": "Kim", "other_hidden": True, "send_disabled": False})
        self.assertEqual(out["other_empty"],
                         {"selected": "__other__", "other_hidden": False, "send_disabled": True})
        self.assertEqual(out["other_spaces"],
                         {"selected": "__other__", "other_hidden": False, "send_disabled": True})
        self.assertEqual(out["after_disabled_click"], {"posts": 0, "open": True})
        self.assertEqual(out["other_named"],
                         {"selected": "__other__", "other_hidden": False, "send_disabled": False})
        self.assertEqual(out["team_again"],
                         {"selected": "Kim", "other_hidden": True, "send_disabled": False})

    SEND_TAIL = """
var out = {};
click(b);
regenerateSend(b, %s, %s);
out.after_send = { panel: regenPanel(b), disabled: !!b.disabled,
                   status: b.parentNode.children[1].textContent };
afterTicks(function () {
    out.posts = POSTS;
    out.status = b.parentNode.children[1].textContent;
    out.disabled = !!b.disabled;
    out.stored = typeof STORE === 'undefined' ? null : (STORE['script-map-author'] || null);
    console.log(JSON.stringify(out));
});
"""

    def send(self, name, guidance, **kw):
        return self.run_panel_in_node(
            self.SEND_TAIL % (json.dumps(name), json.dumps(guidance)), **kw)

    def test_regenerate_send_posts_team_name_and_trimmed_guidance(self):
        out = self.send("Kim", "  Focus on\nthe holds.  ", team=["Alex", "Kim"],
                        prelude=STORAGE_PRELUDE % "{}")
        self.assertEqual(len(out["posts"]), 1)
        post = out["posts"][0]
        self.assertEqual(post["url"], "/api/rebuild")
        self.assertEqual(post["init"]["method"], "POST")
        self.assertEqual(post["init"]["headers"], {"content-type": "application/json"})
        self.assertEqual(json.loads(post["init"]["body"]),
                         {"key": "RTMH:ue.js", "author": "Kim",
                          "guidance": "Focus on\nthe holds."})
        self.assertEqual(out["after_send"], {"panel": None, "disabled": True, "status": ""})
        self.assertEqual(out["status"], "Regeneration requested. New summary in about "
                                        "10 minutes, then reload this page.")
        self.assertTrue(out["disabled"])
        self.assertEqual(out["stored"], "Kim")

    def test_regenerate_send_posts_other_name_trimmed(self):
        out = self.send("  Lee Ann  ", "   ", team=["Alex"], prelude=STORAGE_PRELUDE % "{}")
        self.assertEqual(json.loads(out["posts"][0]["init"]["body"]),
                         {"key": "RTMH:ue.js", "author": "Lee Ann", "guidance": ""})
        self.assertEqual(out["stored"], "Lee Ann")

    def test_regenerate_send_error_reenables_and_panel_reopens(self):
        tail = """
var out = {};
regenerateSend(b, 'Kim', '');
out.during = !!b.disabled;
afterTicks(function () {
    out.status = b.parentNode.children[1].textContent;
    out.disabled = !!b.disabled;
    click(b);
    out.reopened = panelState(b);
    console.log(JSON.stringify(out));
});
"""
        out = self.run_panel_in_node(
            tail, team=["Kim"],
            reply={"status": 429, "json": {"requested_at": "2026-10-12T09:58:00Z"}})
        self.assertTrue(out["during"])
        self.assertEqual(out["status"],
                         "Already requested at 09:58 UTC. Try again after 10:08 UTC.")
        self.assertFalse(out["disabled"])
        self.assertEqual(out["reopened"]["selected"], "Kim")
        self.assertFalse(out["reopened"]["send_disabled"])

    def test_regenerate_remembered_name_preselects(self):
        tail = "click(b); console.log(JSON.stringify(panelState(b)));"
        cases = [
            ({"script-map-author": " Kim "}, ["Alex", "Kim"], "Kim", True, ""),
            ({"script-map-author": "Zed"}, ["Alex", "Kim"], "__other__", False, "Zed"),
            ({"script-map-author": "  "}, ["Alex", "Kim"], "Alex", True, ""),
            ({}, ["Alex", "Kim"], "Alex", True, ""),
            ({}, [], "__other__", False, ""),
            ({"script-map-author": "Zed"}, [], "__other__", False, "Zed"),
        ]
        for store, team, selected, hidden, value in cases:
            with self.subTest(store=store, team=team):
                state = self.run_panel_in_node(tail, team=team,
                                               prelude=STORAGE_PRELUDE % json.dumps(store))
                self.assertEqual(state["selected"], selected)
                self.assertEqual(state["other"]["hidden"], hidden)
                self.assertEqual(state["other"]["value"], value)
                self.assertEqual(state["send_disabled"], not (value or selected != "__other__"))

    def test_regenerate_throwing_storage_still_works(self):
        out = self.send("Kim", "", team=["Alex", "Kim"], prelude=THROWING_STORAGE_PRELUDE)
        self.assertEqual(json.loads(out["posts"][0]["init"]["body"]),
                         {"key": "RTMH:ue.js", "author": "Kim", "guidance": ""})
        self.assertTrue(out["disabled"])
        state = self.run_panel_in_node("click(b); console.log(JSON.stringify(panelState(b)));",
                                       team=["Alex", "Kim"], prelude=THROWING_STORAGE_PRELUDE)
        self.assertEqual(state["selected"], "Alex")

    def test_regenerate_without_window_defaults(self):
        state = self.run_panel_in_node("click(b); console.log(JSON.stringify(panelState(b)));",
                                       team=["Alex"])
        self.assertEqual(state["selected"], "Alex")

    def test_regenerate_team_missing_or_invalid_is_other_only(self):
        tail = "click(b); console.log(JSON.stringify(panelState(b)));"
        for edit in ("delete d.team;", "d.team = 'Kim';", "d.team = null;", "d.team = [];"):
            with self.subTest(edit=edit):
                state = self.run_panel_in_node(tail, edit=edit)
                self.assertEqual(state["options"], [["__other__", "Other…"]])
                self.assertEqual(state["selected"], "__other__")
                self.assertFalse(state["other"]["hidden"])
                self.assertTrue(state["send_disabled"])

    def test_regenerate_regenerate_shim_with_team_name(self):
        # The shared regenerate() helper picks a team option when the name is listed.
        result = self.run_header_in_node(
            ["--refresh-url", "/api/rebuild", "--team", "Kim"],
            "regenerate([{\"status\": 202}], 'Kim', ' x ');")
        self.assertEqual(json.loads(result["calls"][0]["init"]["body"]),
                         {"key": "RTMH:ue.js", "author": "Kim", "guidance": "x"})

    HOW_TAIL = "console.log(JSON.stringify(summaryLabels()));"

    def test_ai_summary_how_part_labels(self):
        names = ("a.js", "b.js", "c.js", "d.js", "e.js", "f.js", "g.js", "h.js", "i.js", "j.js")
        repos = {"RTMH": {n: USER_EVENT for n in names}}
        stamp = "2026-03-04T05:06:07Z"
        values = {
            "a.js": ["ai", "first", "", stamp],
            "b.js": ["ai", "code_changed", "", stamp],
            "c.js": ["ai", "regenerate", "Kim", stamp],
            "d.js": ["ai", "regenerate", "", stamp],
            "e.js": ["ai", "reused", "", stamp],
            "f.js": ["ai", "weird", "Kim", stamp],
            "g.js": ["ai", "", "", stamp],
            "h.js": ["ai", "first", "Kim", ""],
            "i.js": ["header", "regenerate", "Kim", stamp],
            "j.js": ["ai", "regenerate", "Lee Ann", "not a date"],
        }
        prelude = ("editData(function (d) { var v = %s; d.nodes.forEach(function (n) {"
                   " if (v[n.name]) { n.summary = 'Text.'; n.summary_source = v[n.name][0];"
                   " n.summary_trigger = v[n.name][1]; n.summary_author = v[n.name][2];"
                   " n.summary_generated_at = v[n.name][3]; } }); });" % json.dumps(values))
        labels = self.run_header_in_node([], self.HOW_TAIL, prelude, repos)
        gen = " · generated 2026-03-04 05:06 UTC"
        self.assertEqual(labels, {
            "a.js": [" · AI summary · first summary" + gen],
            "b.js": [" · AI summary · updated after code change" + gen],
            "c.js": [" · AI summary · regenerated by Kim" + gen],
            "d.js": [" · AI summary · regenerated" + gen],
            "e.js": [" · AI summary · copied from identical script" + gen],
            "f.js": [" · AI summary" + gen],
            "g.js": [" · AI summary" + gen],
            "h.js": [" · AI summary · first summary"],
            "i.js": [" · From header comment"],
            "j.js": [" · AI summary · regenerated by Lee Ann"],
        })

    def test_ai_summary_how_part_via_cache(self):
        sha = hashlib.sha256(USER_EVENT.encode("utf-8")).hexdigest()
        cache = self.write_cache({"RTMH:ai.js": dict(
            self.summary_entry(sha, "Holds."), generated_at="2026-01-02T03:04:05Z",
            trigger="regenerate", author="Kim", guidance="Mention holds.")})
        labels = self.run_header_in_node(
            ["--summaries", str(cache)], self.HOW_TAIL, "", {"RTMH": {"ai.js": USER_EVENT}})
        self.assertEqual(labels, {
            "ai.js": [" · AI summary · regenerated by Kim · generated 2026-01-02 03:04 UTC"]})

    def run_extras(self, values, argv=()):
        repos = {"RTMH": {n: USER_EVENT for n in values}}
        prelude = ("editData(function (d) { var v = %s; d.nodes.forEach(function (n) {"
                   " if (v[n.name]) { Object.keys(v[n.name]).forEach(function (k) {"
                   " n[k] = v[n.name][k]; }); } }); });" % json.dumps(values))
        return self.run_header_in_node(
            list(argv), "console.log(JSON.stringify(summaryExtras()));", prelude, repos)

    def test_guidance_line(self):
        base = {"summary": "Text.", "summary_source": "ai", "summary_trigger": "regenerate",
                "summary_author": "Kim", "summary_history": []}
        out = self.run_extras({
            "a.js": dict(base, summary_guidance="Mention <b>holds</b> & \"quotes\"."),
            "b.js": dict(base, summary_guidance=""),
            "c.js": dict(base, summary_source="header", summary_guidance="Ignored."),
        })
        self.assertEqual(out["a.js"]["order"], ["span", "div.kind"])
        self.assertEqual(out["a.js"]["guidance"],
                         ['Guidance used: "Mention <b>holds</b> & "quotes"."'])
        self.assertIsNone(out["a.js"]["history"])
        self.assertEqual(out["b.js"]["order"], ["span"])
        self.assertEqual(out["b.js"]["guidance"], [])
        self.assertEqual(out["c.js"]["order"], ["span"])

    def test_guidance_and_history_sit_above_regenerate(self):
        out = self.run_extras({"a.js": {
            "summary": "Text.", "summary_source": "ai", "summary_guidance": "G.",
            "summary_history": [{"summary": "Old.", "generated_at": "", "trigger": "first",
                                 "author": "", "guidance": ""}]}},
            argv=["--refresh-url", "/api/rebuild"])
        self.assertEqual(out["a.js"]["order"],
                         ["span", "div.kind", "details.summary-history", "div.regen"])

    def test_summary_history_section(self):
        history = [
            {"summary": "Newest.", "generated_at": "2026-02-01T10:20:30Z",
             "trigger": "regenerate", "author": "Kim", "guidance": "Mention holds."},
            {"summary": "Middle.", "generated_at": "2026-01-15T08:00:00Z",
             "trigger": "", "author": "", "guidance": ""},
            {"summary": "Older.", "generated_at": "not a date",
             "trigger": "code_changed", "author": "", "guidance": ""},
            {"summary": "Bare.", "generated_at": "", "trigger": "", "author": "",
             "guidance": ""},
            {"summary": "Oldest.", "generated_at": "", "trigger": "regenerate",
             "author": "", "guidance": "x" * 500},
        ]
        out = self.run_extras({
            "a.js": {"summary": "Now.", "summary_source": "ai", "summary_trigger": "first",
                     "summary_history": history},
            "b.js": {"summary": "Now.", "summary_source": "ai", "summary_history": []},
            "c.js": {"summary": "Now.", "summary_source": "ai", "summary_history": "bad"},
        })
        self.assertEqual(out["a.js"]["order"], ["span", "details.summary-history"])
        self.assertEqual(out["a.js"]["history"], {
            "tag": "details",
            "open": False,
            "summary": "summary:History (5)",
            "items": [
                [["", "Newest."],
                 ["kind", "regenerated by Kim · generated 2026-02-01 10:20 UTC"],
                 ["kind", 'Guidance used: "Mention holds."']],
                [["", "Middle."], ["kind", "generated 2026-01-15 08:00 UTC"]],
                [["", "Older."], ["kind", "updated after code change"]],
                [["", "Bare."]],
                [["", "Oldest."], ["kind", "regenerated"],
                 ["kind", 'Guidance used: "' + "x" * 500 + '"']],
            ],
        })
        for name in ("b.js", "c.js"):
            with self.subTest(name=name):
                self.assertIsNone(out[name]["history"])
                self.assertEqual(out[name]["order"], ["span"])

    def test_template_panel_and_history_css(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        media = text[text.index("@media (max-width: 600px) {"):]
        media = media[:media.index("\n}\n")]
        self.assertIn(".regen-panel select, .regen-panel input, .regen-panel textarea "
                      "{ width: 100%; box-sizing: border-box; }", media)
        panel = self.css_rule(text, ".regen-panel")
        self.assertEqual(panel["border"], "1px solid var(--border)")
        self.assertEqual(panel["max-width"], "100%")
        fields = self.css_rule(text, ".regen-panel select, .regen-panel input, "
                                     ".regen-panel textarea")
        self.assertEqual(fields["max-width"], "100%")
        self.assertEqual(fields["color"], "var(--text)")
        history = self.css_rule(text, ".summary-history")
        self.assertEqual(history["overflow-wrap"], "anywhere")
        self.assertEqual(history["word-break"], "break-word")
        # New rules use only the palette variables, so both themes apply.
        for selector in (".regen-panel", ".regen-panel select, .regen-panel input, "
                         ".regen-panel textarea", ".summary-history"):
            for value in self.css_rule(text, selector).values():
                self.assertNotRegex(value, r"#[0-9a-fA-F]{3,6}\b")

    def test_template_has_no_unsafe_dom_writes(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertEqual(text.count("<script"), 2)
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                    "http://", "https://", "querySelector('#script-search"):
            self.assertNotIn(bad, text)


DOM_SHIM = r"""
function Elem(tag) {
    this.tagName = tag; this.children = []; this.parentNode = null; this.className = '';
    this.attrs = {}; this.text = ''; this.hidden = false; this.value = ''; this.style = {};
    var self = this;
    this.classList = { toggle: function () {}, add: function () {}, remove: function () {} };
}
Object.defineProperty(Elem.prototype, 'textContent', {
    get: function () { return this.text + this.children.map(function (c) { return c.textContent; }).join(''); },
    set: function (v) { this.text = String(v); this.children = []; }
});
Object.defineProperty(Elem.prototype, 'firstChild', {
    get: function () { return this.children[0] || null; }
});
Object.defineProperty(Elem.prototype, 'nextSibling', {
    get: function () {
        if (!this.parentNode) { return null; }
        var sibs = this.parentNode.children;
        return sibs[sibs.indexOf(this) + 1] || null;
    }
});
Elem.prototype.appendChild = function (c) {
    if (c.parentNode) { c.parentNode.removeChild(c); }
    c.parentNode = this; this.children.push(c); return c;
};
Elem.prototype.insertBefore = function (c, ref) {
    if (c.parentNode) { c.parentNode.removeChild(c); }
    c.parentNode = this;
    var i = ref ? this.children.indexOf(ref) : -1;
    if (i === -1) { this.children.push(c); } else { this.children.splice(i, 0, c); }
    return c;
};
Elem.prototype.removeChild = function (c) {
    this.children.splice(this.children.indexOf(c), 1); c.parentNode = null; return c;
};
Elem.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
Elem.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
Elem.prototype.addEventListener = function (type, fn) {
    (this.listeners = this.listeners || {});
    (this.listeners[type] = this.listeners[type] || []).push(fn);
};
Elem.prototype.querySelector = function () { return null; };
Elem.prototype.querySelectorAll = function () { return []; };
function Text(t) { this.textContent = t; this.parentNode = null; this.children = []; }
Object.setPrototypeOf(Elem.prototype, Node.prototype);
Object.setPrototypeOf(Text.prototype, Node.prototype);

var byIdMap = {};
var header = new Elem('header');
var document = {
    title: '',
    createElement: function (t) { return new Elem(t); },
    createTextNode: function (t) { return new Text(t); },
    getElementById: function (id) {
        if (!byIdMap[id]) { byIdMap[id] = new Elem('div'); byIdMap[id].id = id; }
        return byIdMap[id];
    },
    querySelectorAll: function () { return []; }
};
function Node() {}
var setInterval = function () {};
var fetch = function () { throw new Error('no network'); };

function setup() {
    ['title', 'generated', 'repo-chips'].forEach(function (id, i) {
        var e = new Elem(['h1', 'span', 'div'][i]); e.id = id; byIdMap[id] = e; header.appendChild(e);
    });
    document.getElementById('script-map-data').text = DATA_TEXT;
}

function report() {
    function find(cls) {
        return header.children.filter(function (c) { return c.className.split(' ').indexOf(cls) !== -1; })[0];
    }
    var badge = find('env-badge');
    var toggle = find('env-toggle');
    var chips = byIdMap['repo-chips'];
    var missing = chips.children.filter(function (c) { return c.className === 'missing-repos'; })[0];
    var chipEls = chips.children.filter(function (c) { return c.className.indexOf('chip') === 0; });
    console.log(JSON.stringify({
        document_title: document.title,
        order: header.children.map(function (c) {
            return c.tagName + (c.className ? '.' + c.className.split(' ')[0] : '');
        }),
        badge: badge ? { 'class': badge.className, text: badge.textContent } : null,
        toggle: toggle ? toggle.children.map(function (c) {
            return { tag: c.tagName, 'class': c.className, text: c.textContent,
                     href: c.getAttribute('href'), 'aria-current': c.getAttribute('aria-current') };
        }) : null,
        chips: chipEls.map(function (c) { return c.textContent; }),
        chip_classes: chipEls.map(function (c) { return c.className; }),
        missing: missing ? missing.textContent : null
    }));
}

function walk(root, pred, out) {
    out = out || [];
    (root.children || []).forEach(function (c) {
        if (pred(c)) { out.push(c); }
        walk(c, pred, out);
    });
    return out;
}

// Fire every listener of one event type on a shim element.
function fire(target, type) {
    ((target.listeners || {})[type] || []).forEach(function (fn) { fn({}); });
}

function regenPanel(button) {
    var p = button.parentNode.children[2];
    return p && p.className === 'regen-panel' ? p : null;
}

// Open the panel, make sure `name` is the effective author (choosing the team option
// when present, else Other… plus typed text), fill in `guidance` and click Send.
function regenerateSend(button, name, guidance) {
    if (!regenPanel(button)) { click(button); }
    var panel = regenPanel(button);
    var select = panel.children[0];
    var hasTeam = select.children.some(function (o) { return o.value === name; });
    select.value = hasTeam ? name : '__other__';
    fire(select, 'change');
    if (!hasTeam) {
        panel.children[1].value = name;
        fire(panel.children[1], 'input');
    }
    panel.children[2].value = guidance || '';
    click(panel.children[4].children[0]);
}

function regenerate(replies, name, guidance) {
    name = name === undefined ? 'Kim' : name;
    var list = byIdMap['i-list'];
    var entries = walk(list, function (c) { return c.tagName === 'details'; });
    var buttons = walk(list, function (c) {
        return c.tagName === 'button' && c.textContent === 'Regenerate summary';
    });
    var summaryRows = walk(list, function (c) { return c.tagName === 'summary'; });
    var inSummaryRow = summaryRows.some(function (row) {
        return walk(row, function (c) { return c.tagName === 'button'; }).length > 0;
    });
    var calls = [];
    var clicks = [];
    var button = buttons[0];
    var status = button ? button.parentNode.children[1] : null;
    function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }
    (async function () {
        for (var i = 0; i < replies.length; i++) {
            var reply = replies[i];
            fetch = function (url, init) {
                calls.push({ url: url, init: init });
                if (reply['throw']) { return Promise.reject(new Error('offline')); }
                return Promise.resolve({
                    status: reply.status,
                    json: function () {
                        return reply.bad_json ? Promise.reject(new SyntaxError('bad'))
                            : Promise.resolve(reply.json);
                    }
                });
            };
            button.disabled = false;
            regenerateSend(button, name, guidance);
            var during = button.disabled;
            for (var k = 0; k < 5; k++) { await tick(); }
            clicks.push({ disabled_during: during, status: status.textContent,
                          disabled: button.disabled });
        }
        console.log(JSON.stringify({
            entries: entries.length,
            buttons: buttons.length,
            button_text: button ? button.textContent : null,
            status_role: status ? status.getAttribute('role') : null,
            in_summary_row: inSummaryRow,
            calls: calls,
            clicks: clicks
        }));
    }());
}

function click(target) {
    ((target.listeners || {}).click || []).forEach(function (fn) { fn({}); });
}

function typeSearch(q) {
    var input = byIdMap['script-search'];
    input.value = q;
    ((input.listeners || {}).input || []).forEach(function (fn) { fn({}); });
}

function enableQuerySelector() {
    Elem.prototype.querySelector = function (tag) {
        return walk(this, function (c) { return c.tagName === tag; })[0] || null;
    };
}

function editData(fn) {
    var d = JSON.parse(DATA_TEXT);
    fn(d);
    DATA_TEXT = JSON.stringify(d);
}

// Fake fetch for the load-time status GET: records calls and answers with `reply`
// ({status, json} | {status, bad_json} | {reject} | {sync_throw}).
var STATUS_CALLS = [];
function statusFetch(reply) {
    fetch = function (url, init) {
        STATUS_CALLS.push({ url: url, init: init });
        if (reply.sync_throw) { throw new Error('offline'); }
        if (reply.reject) { return Promise.reject(new Error('offline')); }
        return Promise.resolve({
            status: reply.status,
            json: function () {
                return reply.bad_json ? Promise.reject(new SyntaxError('bad'))
                    : Promise.resolve(reply.json);
            }
        });
    };
}

function afterTicks(fn) {
    (async function () {
        for (var k = 0; k < 5; k++) { await new Promise(function (r) { setTimeout(r, 0); }); }
        fn();
    }());
}

function entryName(d) {
    return walk(d, function (c) { return c.className === 'name'; })[0].textContent;
}

function summaryLabels() {
    var out = {};
    walk(byIdMap['i-list'], function (c) { return c.tagName === 'details'; }).forEach(function (d) {
        out[entryName(d)] = walk(d, function (c) {
            return c.className === 'kind' && /^ · (AI summary|From header comment)/.test(c.textContent);
        }).map(function (c) { return c.textContent; });
    });
    return out;
}

function regenStatuses(id) {
    var out = {};
    walk(byIdMap[id], function (c) { return c.tagName === 'details'; }).forEach(function (d) {
        var status = walk(d, function (c) { return c.className === 'regen-status'; })[0];
        if (!status) { out[entryName(d)] = null; return; }
        var button = status.parentNode.children[0];
        out[entryName(d)] = { status: status.textContent, disabled: !!button.disabled,
                              status_is_second: status.parentNode.children[1] === status };
    });
    return out;
}

function areaCard(name) {
    return byIdMap['rec-cards'].children.filter(function (c) {
        return c.children[0].textContent === name;
    })[0];
}

function statusReport() {
    var rs = walk(header, function (c) { return c.id === 'refresh-status'; })[0];
    return { calls: STATUS_CALLS, refresh_status: rs ? rs.textContent : null,
             inventory: regenStatuses('i-list') };
}

// Nav buttons for view switching: document.querySelectorAll('nav button') returns them.
var NAV = [];
function enableNav() {
    NAV = ['inventory', 'records', 'risk'].map(function (v) {
        var b = new Elem('button'); b.setAttribute('data-view', v); return b;
    });
    document.querySelectorAll = function (sel) { return sel === 'nav button' ? NAV : []; };
}
function navTo(view) {
    click(NAV.filter(function (b) { return b.getAttribute('data-view') === view; })[0]);
}

// Fake fetch for Regenerate POSTs, installed after load: records calls, answers `reply`.
var POSTS = [];
function postFetch(reply) {
    fetch = function (url, init) {
        POSTS.push({ url: url, init: init });
        if (reply['throw']) { return Promise.reject(new Error('offline')); }
        return Promise.resolve({ status: reply.status, json: function () {
            return Promise.resolve(reply.json);
        } });
    };
}

function regenButton(id) {
    return walk(byIdMap[id || 'i-list'], function (c) {
        return c.tagName === 'button' && c.textContent === 'Regenerate summary';
    })[0] || null;
}

function panelState(button) {
    var p = regenPanel(button);
    if (!p) { return null; }
    var sel = p.children[0], other = p.children[1], g = p.children[2];
    var help = p.children[3], actions = p.children[4];
    return {
        wrap: button.parentNode.children.map(function (c) { return c.tagName + '.' + c.className; }),
        tags: p.children.map(function (c) { return c.tagName; }),
        select_label: sel.getAttribute('aria-label'),
        options: sel.children.map(function (o) { return [o.value, o.textContent]; }),
        selected: sel.value,
        other: { type: other.type, label: other.getAttribute('aria-label'),
                 maxlength: other.getAttribute('maxlength'), hidden: !!other.hidden,
                 value: other.value },
        guidance: { label: g.getAttribute('aria-label'), maxlength: g.getAttribute('maxlength'),
                    placeholder: g.getAttribute('placeholder') },
        help: help.tagName + ':' + help.textContent,
        actions: actions.children.map(function (c) { return [c.type, c.textContent]; }),
        send_disabled: !!actions.children[0].disabled
    };
}

function scriptEntries(id) {
    return walk(byIdMap[id || 'i-list'], function (c) {
        return c.tagName === 'details' && c.getAttribute('data-id') !== null;
    });
}

// What follows the summary text: guidance line(s) and the history section.
function summaryExtras(id) {
    var out = {};
    scriptEntries(id).forEach(function (d) {
        var dl = walk(d, function (c) { return c.tagName === 'dl'; })[0];
        var value = dl.children[1].children[0];
        var top = value.tagName === 'div' ? value.children : [value];
        var hist = top.filter(function (c) { return c.className === 'summary-history'; })[0];
        out[entryName(d)] = {
            order: top.map(function (c) { return c.tagName + (c.className ? '.' + c.className : ''); }),
            guidance: top.filter(function (c) { return c.className === 'kind'; })
                .map(function (c) { return c.textContent; }),
            history: hist ? {
                tag: hist.tagName,
                open: !!hist.open,
                summary: hist.children[0].tagName + ':' + hist.children[0].textContent,
                items: hist.children[1].children.map(function (li) {
                    return li.children.map(function (c) { return [c.className, c.textContent]; });
                })
            } : null
        };
    });
    return out;
}

function themeButton() {
    return header.children.filter(function (c) { return c.id === 'theme-toggle'; })[0] || null;
}

function themeState() {
    var b = themeButton();
    return {
        tag: b ? b.tagName : null,
        type: b ? b.type : null,
        text: b ? b.textContent : null,
        aria: b ? b.getAttribute('aria-label') : null,
        last: header.children[header.children.length - 1] === b,
        data_theme: document.documentElement ?
            document.documentElement.getAttribute('data-theme') : null,
        stored: typeof STORE === 'undefined' ? null : STORE['script-map-theme'] || null
    };
}

function snapshot() {
    function textOf(c) { return c.textContent; }
    function names(id) {
        return walk(byIdMap[id], function (c) { return c.className === 'name'; }).map(textOf);
    }
    function empties(id) {
        return walk(byIdMap[id], function (c) { return c.className === 'empty'; }).map(textOf);
    }
    return {
        search: byIdMap['script-search'].value,
        options: byIdMap['script-search-options'].children.map(function (o) {
            return { tag: o.tagName, value: o.value };
        }),
        list_heading: byIdMap['i-list-heading'].textContent,
        list: names('i-list'),
        list_empty: empties('i-list'),
        tiles: byIdMap['i-tiles'].textContent,
        matrix: byIdMap['i-matrix'].textContent,
        risk: names('r-grid'),
        risk_empty: empties('r-grid'),
        areas: byIdMap['rec-cards'].children.map(function (c) { return c.children.map(textOf); }),
        rec_chips: byIdMap['rec-chips'].children.map(textOf),
        rec_list_heading: byIdMap['rec-list-heading'].textContent,
        rec_list: names('rec-list'),
        lanes: byIdMap['rec-detail'].children.filter(function (c) {
            return c.className === 'lane';
        }).map(function (c) {
            return [c.children[0].textContent].concat(c.children[1].children.map(function (li) {
                return li.children[0].textContent;
            }));
        }),
        missing_count: byIdMap['rec-missing-count'].textContent,
        missing: byIdMap['rec-missing-list'].children.map(textOf)
    };
}
"""


if __name__ == "__main__":
    unittest.main()
