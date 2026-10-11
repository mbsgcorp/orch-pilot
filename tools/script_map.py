#!/usr/bin/env python3
"""Render a self-contained HTML map of the SuiteScript files in one or more repos.

Usage: python3 tools/script_map.py --repo LABEL=PATH [--repo LABEL=PATH ...]
                                   --out OUT.html [--title TEXT]

Each PATH must be an existing directory. If PATH/.git is a directory, its HEAD
file must read exactly "ref: refs/heads/main"; any other branch or a detached
HEAD prints an error and exits 2 without writing anything. If PATH/.git is
missing or is a file, the run proceeds with a "branch not verified" warning.
Git itself is never invoked.

Every *.js file under PATH is scanned (directory symlinks are not followed;
.git, node_modules, backup(s), temp-handoffs, test(s) and sandbox folders and
*.min.js files are excluded). Files are classified as SuiteScript by their
@NScriptType / @NApiVersion tags or nlapi calls, then parsed for entry points,
"DEPLOY ON:" record targets, touched record types, define([...]) library
dependencies, clientScriptModulePath and a simple risk score. The result is
embedded as JSON in tools/script_map_template.html and written to OUT. On
success one summary line is printed and the exit code is 0; every input or
branch error prints to stderr and exits 2.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "script_map_template.html"
DATA_TOKEN = "__SCRIPT_MAP_DATA__"
MAX_BYTES = 2_000_000

EXCLUDED_DIRS = {
    ".git", "node_modules", "backups", "backup", "temp-handoffs",
    "tests", "test", "sandbox",
}

ENTRY_POINTS = [
    "beforeLoad", "beforeSubmit", "afterSubmit", "pageInit", "fieldChanged",
    "postSourcing", "sublistChanged", "lineInit", "validateField",
    "validateLine", "validateInsert", "validateDelete", "saveRecord",
    "onRequest", "get", "post", "put", "delete", "getInputData", "map",
    "reduce", "summarize", "execute", "render", "onAction",
]

XRAY_LANES = [
    "beforeLoad", "beforeSubmit", "afterSubmit", "pageInit", "fieldChanged",
    "postSourcing", "sublistChanged", "lineInit", "validateField",
    "validateLine", "validateInsert", "validateDelete", "saveRecord",
]

TYPE_BLOCKLIST = {
    "text", "textarea", "select", "multiselect", "checkbox", "date",
    "datetime", "datetimetz", "timeofday", "currency", "float", "integer",
    "percent", "phone", "email", "url", "inlinehtml", "longtext", "richtext",
    "label", "help", "hidden", "file", "image", "password", "submit",
    "button", "list", "inline", "normal", "entry", "staticlist", "editor",
    "plain", "radio", "inlineeditor",
}

SCRIPT_TYPE_RE = re.compile(r"@NScriptType\s+(\w+)")
API_VERSION_RE = re.compile(r"@NApiVersion\s+([^\s*]+)")
RETURN_RE = re.compile(r"\breturn\s*\{")
KEY_RE = re.compile(r"(?:^|[{,])\s*([A-Za-z_$][\w$]*)\s*(?=[,:}]|$)")
DEPLOY_RE = re.compile(r"DEPLOY ON:(.*)", re.IGNORECASE)
ID_RE = re.compile(r"[A-Za-z0-9_]+")
PAREN_RE = re.compile(r"\(([^)]*)\)")
TYPE_ENUM_RE = re.compile(r"\b(?:record|search)\.Type\.([A-Za-z0-9_]+)")
TYPE_LITERAL_RE = re.compile(r"\btype\s*:\s*(['\"])([^'\"\n]*)\1")
RECORD_ID_RE = re.compile(r"[a-z][a-z0-9_]*")
CUSTOMRECORD_RE = re.compile(r"(['\"])(customrecord[a-z0-9_]*)\1", re.IGNORECASE)
DEFINE_RE = re.compile(r"\bdefine\s*\(\s*\[")
STRING_RE = re.compile(r"['\"]([^'\"]+)['\"]")
CLIENT_PATH_RE = re.compile(r"\bclientScriptModulePath\s*=\s*(['\"])([^'\"]+)\1")
TRY_RE = re.compile(r"\btry\s*\{")
HARDCODED_ID_RE = re.compile(r"(?i)\b(?:id|internalid)\s*[:=]\s*['\"]?\d+\b")
SHA_RE = re.compile(r"[0-9a-fA-F]{40}([0-9a-fA-F]{24})?")


def parse_repo_arg(value):
    """Return (label, Path) for a LABEL=PATH value; raise ValueError if malformed."""
    if "=" not in value:
        raise ValueError(f"--repo {value!r} must be LABEL=PATH")
    label, path = value.split("=", 1)
    if not label.strip():
        raise ValueError(f"--repo {value!r} has an empty label")
    if not path:
        raise ValueError(f"--repo {value!r} has an empty path")
    return label, Path(path)


def branch_status(path):
    """Return "main", None (not verified), or the offending branch name.

    Raises OSError if PATH/.git is a directory but its HEAD cannot be read.
    """
    git = Path(path) / ".git"
    if not git.is_dir():
        return None
    head = (git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    if head == "ref: refs/heads/main":
        return "main"
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):]
    if SHA_RE.fullmatch(head):
        return "detached HEAD"
    return head[len("ref: "):] if head.startswith("ref: ") else (head or "unknown HEAD")


def classify(text):
    """Return (is_suitescript, script_type, api_version)."""
    has_nlapi = "nlapi" in text
    if "@NScriptType" not in text and "@NApiVersion" not in text and not has_nlapi:
        return False, "", ""
    match = SCRIPT_TYPE_RE.search(text)
    if match:
        script_type = match.group(1)
    elif has_nlapi:
        script_type = "SuiteScript 1.0"
    else:
        script_type = "Library"
    match = API_VERSION_RE.search(text)
    if match:
        api_version = match.group(1)
    elif has_nlapi:
        api_version = "1.0"
    else:
        api_version = ""
    return True, script_type, api_version


def entry_points(text):
    """Return allow-listed keys of the object in the last `return {` statement."""
    matches = list(RETURN_RE.finditer(text))
    if not matches:
        return []
    start = matches[-1].end() - 1
    depth = 0
    end = None
    for i in range(start, len(text)):
        char = text[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        return []
    body = text[start:end + 1]
    allowed = set(ENTRY_POINTS)
    found = []
    for match in KEY_RE.finditer(body):
        key = match.group(1)
        if key in allowed and key not in found:
            found.append(key)
    return found


def deploy_targets(text):
    """Return [(record_id, label), ...] from every `DEPLOY ON:` line, deduplicated."""
    targets = []
    seen = set()
    for line in text.split("\n"):
        match = DEPLOY_RE.search(line)
        if not match:
            continue
        for part in match.group(1).split(","):
            id_match = ID_RE.search(part)
            if not id_match:
                continue
            record_id = id_match.group(0).lower()
            paren = PAREN_RE.search(part)
            label = paren.group(1).strip() if paren and paren.group(1).strip() else record_id
            if record_id not in seen:
                seen.add(record_id)
                targets.append((record_id, label))
    return targets


def touched_records(text):
    """Return record ids referenced by record/search.Type, type: literals and customrecord strings."""
    found = []

    def add(record_id):
        if record_id not in found:
            found.append(record_id)

    for match in TYPE_ENUM_RE.finditer(text):
        add(match.group(1).lower().replace("_", ""))
    for match in TYPE_LITERAL_RE.finditer(text):
        value = match.group(2)
        if RECORD_ID_RE.fullmatch(value) and value not in TYPE_BLOCKLIST:
            add(value)
    for match in CUSTOMRECORD_RE.finditer(text):
        add(match.group(2).lower())
    return found


def dependencies(text):
    """Return (relative_paths, N/ modules) from the first define([...]) array."""
    match = DEFINE_RE.search(text)
    if not match:
        return [], []
    close = text.find("]", match.end())
    if close == -1:
        return [], []
    paths = []
    modules = []
    for item in STRING_RE.findall(text[match.end():close]):
        if item.startswith(("./", "../", "/")):
            if item not in paths:
                paths.append(item)
        elif item.startswith("N/"):
            if item not in modules:
                modules.append(item)
    return paths, modules


def client_module_path(text):
    """Return every clientScriptModulePath string assigned in text."""
    paths = []
    for match in CLIENT_PATH_RE.finditer(text):
        if match.group(2) not in paths:
            paths.append(match.group(2))
    return paths


def risk(text, record_count):
    """Return {"score", "level", "reasons"} for a script's text."""
    score = 0
    reasons = []
    if "nlapi" in text:
        score += 3
        reasons.append("SuiteScript 1.0")
    if not TRY_RE.search(text):
        score += 2
        reasons.append("no try/catch")
    lines = len(text.splitlines())
    if lines > 1500:
        score += 2
        reasons.append(f"{lines:,} lines")
    hardcoded = len(HARDCODED_ID_RE.findall(text))
    if hardcoded > 5:
        score += 1
        reasons.append(f"{hardcoded} hardcoded IDs")
    if record_count >= 3:
        score += 1
        reasons.append(f"touches {record_count} records")
    if score >= 4:
        level = "high"
    elif score >= 2:
        level = "medium"
    else:
        level = "low"
    return {"score": score, "level": level, "reasons": reasons}


def basename_key(path):
    """Lowercase last path segment without a trailing .js."""
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if name.lower().endswith(".js"):
        name = name[:-3]
    return name.lower()


def js_files(root):
    """Yield (relative_posix_path, absolute_path) for candidate *.js files."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in EXCLUDED_DIRS)
        for name in sorted(filenames):
            lower = name.lower()
            if not lower.endswith(".js") or lower.endswith(".min.js"):
                continue
            full = Path(dirpath) / name
            yield full.relative_to(root).as_posix(), full


def collect(repos):
    """Return the JSON-serializable map data for [(label, Path), ...]."""
    repo_rows = []
    nodes = []
    edges = []
    skipped = []
    edge_keys = set()
    unresolved = set()

    def add_edge(source, target, kind):
        key = (source, target, kind)
        if key not in edge_keys:
            edge_keys.add(key)
            edges.append({"source": source, "target": target, "kind": kind})

    for label, root in repos:
        root = Path(root)
        scripts = []
        for rel, full in js_files(root):
            try:
                if os.path.getsize(full) > MAX_BYTES:
                    skipped.append({"repo": label, "path": rel, "reason": "too large"})
                    continue
                raw = full.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                skipped.append({"repo": label, "path": rel,
                                "reason": f"unreadable: {exc.strerror}"})
                continue
            text = raw.replace("\r\n", "\n")
            is_script, script_type, api_version = classify(text)
            if not is_script:
                skipped.append({"repo": label, "path": rel, "reason": "not SuiteScript"})
                continue
            scripts.append((rel, text, script_type, api_version))
        scripts.sort(key=lambda item: item[0])

        by_basename = {}
        for rel, _, _, _ in scripts:
            by_basename.setdefault(basename_key(rel), []).append(f"{label}:{rel}")

        records = {}
        script_nodes = []
        for rel, text, script_type, api_version in scripts:
            node_id = f"{label}:{rel}"
            deploys = deploy_targets(text)
            deploy_ids = [record_id for record_id, _ in deploys]
            touches = [r for r in touched_records(text) if r not in deploy_ids]
            for record_id, record_label in deploys:
                entry = records.setdefault(record_id, None)
                if entry is None and record_label != record_id:
                    records[record_id] = record_label
                add_edge(node_id, f"{label}:{record_id}", "deploy")
            for record_id in touches:
                records.setdefault(record_id, None)
                add_edge(node_id, f"{label}:{record_id}", "touches")

            dep_paths, modules = dependencies(text)
            links = [(path, "library") for path in dep_paths]
            links += [(path, "client") for path in client_module_path(text)]
            for path, kind in links:
                matches = by_basename.get(basename_key(path))
                if matches:
                    for target in matches:
                        add_edge(node_id, target, kind)
                else:
                    target = f"{label}:unresolved:{path}"
                    if target not in unresolved:
                        unresolved.add(target)
                        nodes.append({"id": target, "kind": "unresolved",
                                      "repo": label, "label": path})
                    add_edge(node_id, target, kind)

            script_nodes.append({
                "id": node_id,
                "kind": "script",
                "repo": label,
                "path": rel,
                "name": rel.rsplit("/", 1)[-1],
                "type": script_type,
                "api_version": api_version,
                "entry_points": entry_points(text),
                "modules": modules,
                "has_deploy_line": bool(DEPLOY_RE.search(text)),
                "risk": risk(text, len(set(deploy_ids) | set(touches))),
            })

        nodes.extend(script_nodes)
        for record_id in sorted(records):
            nodes.append({
                "id": f"{label}:{record_id}",
                "kind": "record",
                "repo": label,
                "record_id": record_id,
                "label": records[record_id] or record_id,
            })
        try:
            status = branch_status(root)
        except OSError:
            status = None
        repo_rows.append({
            "label": label,
            "branch": "main" if status == "main" else "branch not verified",
            "script_count": len(script_nodes),
        })

    return {"repos": repo_rows, "nodes": nodes, "edges": edges, "skipped": skipped}


def render(data):
    """Return the HTML page with data embedded in the template."""
    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count(DATA_TOKEN) != 1:
        raise ValueError(f"{TEMPLATE.name} must contain {DATA_TOKEN} exactly once")
    payload = json.dumps(data).replace("</", "<\\/").replace("<!--", "<\\u0021--")
    return template.replace(DATA_TOKEN, payload)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Render a self-contained HTML map of SuiteScript repos.")
    parser.add_argument("--repo", action="append", required=True,
                        metavar="LABEL=PATH",
                        help="repository to scan; repeat for several repos")
    parser.add_argument("--out", required=True, help="HTML file to write")
    parser.add_argument("--title", default="Script map", help="page title")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    repos = []
    labels = set()
    for value in args.repo:
        try:
            label, path = parse_repo_arg(value)
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
            status = branch_status(path)
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

    data = collect(repos)
    data["title"] = args.title
    data["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        page = render(data)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(page, encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"error: cannot write {args.out}: {exc}", file=sys.stderr)
        return 2

    nodes = data["nodes"]
    scripts = sum(1 for n in nodes if n["kind"] == "script")
    records = sum(1 for n in nodes if n["kind"] == "record")
    print(f"script_map: {scripts} scripts, {records} records, {len(data['edges'])} edges, "
          f"{len(data['skipped'])} skipped -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
