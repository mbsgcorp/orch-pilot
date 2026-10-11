import contextlib
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

        def flaky(path):
            calls.append(path)
            if len(calls) > 1:
                (Path(path) / ".git" / "HEAD").unlink()
            return real(path)

        with mock.patch.object(script_map, "branch_status", flaky):
            code, out, err = self.run_main(["--repo", f"A={repo}", "--out", str(self.out)])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith(f"error: A cannot read {repo / '.git' / 'HEAD'}: "), err)
        self.assertFalse(self.out.exists())

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
        self.assertEqual(self.scripts(data)["v1.js"]["type"], "SuiteScript 1.0")

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

    # AC10 / AC16: template

    def test_template_is_self_contained_and_safe(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        for bad in ("http://", "https://", "<link", "innerHTML", "outerHTML",
                    "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(bad, text)
        self.assertEqual(text.count(script_map.DATA_TOKEN), 1)
        for label in ("Constellation", "Record X-ray", "Risk grid", "All repos",
                      "Other scripts that touch this record", "No scripts found",
                      "Order within a step follows deployment order in NetSuite, "
                      "which isn't in the code.", "prefers-color-scheme"):
            self.assertIn(label, text)

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


if __name__ == "__main__":
    unittest.main()
