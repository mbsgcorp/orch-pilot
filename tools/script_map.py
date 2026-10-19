#!/usr/bin/env python3
"""Render a self-contained HTML map of the SuiteScript files in one or more repos.

Usage: python3 tools/script_map.py --repo LABEL=PATH [--repo LABEL=PATH ...]
                                   --out OUT.html [--title TEXT]
                                   [--summaries CACHE.json] [--refresh-url PATH]
                                   [--branch NAME] [--environment NAME]
                                   [--switch-url PATH --switch-label TEXT]
                                   [--missing-repo LABEL ...]
                                   [--summary-namespace NAME] [--team NAME ...]
                                   [--git-info FILE] [--ns-only LABEL ...]

Each PATH must be an existing directory. If PATH/.git is a directory, its HEAD
file must read exactly "ref: refs/heads/NAME", where NAME is --branch (default
main); any other branch or a detached HEAD prints an error and exits 2 without
writing anything. If PATH/.git is missing or is a file, the run proceeds with a
"branch not verified" warning. Git itself is never invoked.

--environment (default Production) is shown as a badge in the page header; any
other value is styled as a warning and added to the page title. --switch-url
and --switch-label add a Prod | Sandbox style toggle linking to the other
page. Each --missing-repo LABEL is listed on a "Not included (no NAME branch)"
line under the repo chips.

Every *.js file under PATH is scanned (directory symlinks are not followed;
.git, node_modules, backup(s), temp-handoffs, test(s) and sandbox folders and
*.min.js files are excluded). Files are classified as SuiteScript by their
@NScriptType / @NApiVersion tags or nlapi calls, then parsed for entry points,
"DEPLOY ON:" record targets, touched record types, define([...]) library
dependencies, clientScriptModulePath and a simple risk score; each record gets
a business-area category (see record_category). A record node's label is its
DEPLOY ON label when one was recorded, else its RECORD_LABELS display name,
else the record id. Touched record types are
record/search.Type.X enums and quoted customrecord* strings, plus `type: '...'`
literals that are known record ids or sit inside a record call's arguments
(IDENT.create/load/... ( ... ), see touched_records); other literals such as
chart types are ignored everywhere, including the risk record count. Untagged
SuiteScript 1.0 files get a script type inferred from their folder names. The
result is embedded as JSON in tools/script_map_template.html and written to
OUT. On success one summary line is printed and the exit code is 0; every
input or branch error prints to stderr and exits 2.

Each script gets a one-line summary: the cached AI summary from --summaries
(written by tools/script_map_summarize.py) when the entry for "LABEL:path"
has a sha256 matching the file, else any cache entry with a matching sha256
(first in key order, any namespace), else the description from the file's header comment, else nothing. This tool
only reads the cache; it never calls the network.

Each script node also carries "summary_key", the summary cache key the
script_map_summarize.py run for this page would write: "NAME|LABEL:path" with
--summary-namespace NAME, else "LABEL:path". The page's Regenerate summary
button sends it to --refresh-url.

Each script node also carries "summary_trigger", "summary_author",
"summary_guidance" and "summary_history" (at most 5 {summary, generated_at,
trigger, author, guidance} objects, newest first) from the same cache entry as
"summary_generated_at"; they are "" and [] for header or empty summaries.
Each --team NAME (repeatable, 1-40 characters after trimming) is listed in the
page's "team" field, de-duplicated and sorted case-insensitively.

--git-info FILE reads {"version": 1, "files": {"LABEL:path": {"commit",
"author", "date"}}} (written by tools/script_map_gitinfo.py). Each script node
gets "last_commit", "last_commit_author" and "last_commit_date" from the entry
whose key equals the node id; they are "" when there is no entry, no
--git-info, or the field is not a string. A missing FILE prints
"error: FILE does not exist" and an invalid one "error: FILE is not a valid git
info file"; both exit 2. Nodes also get "last_commit_subject" and
"last_commit_pr" from the entry's "subject" and "pr", and "commit_history"
from its "history": the previous versions only, i.e. the object items whose
"commit" is not "last_commit" (nothing is dropped when "last_commit" is ""),
at most 5 {commit, author, date, subject, pr} objects, newest first, each
with "commit_url", "pr_url", "file_url" and "raw_url". Nodes also get
"last_commit_file_url" and "last_commit_raw_url", the "blob" and "raw" URLs
of the node's path at "last_commit".
The file's top-level "repos" object maps LABEL to a GitHub "OWNER/REPO" slug;
"last_commit_url", "last_commit_pr_url", "last_commit_file_url",
"last_commit_raw_url" and the history URLs start with LINK_ORIGIN + slug and
are "" unless the slug, the 40-hex commit and the PR number are valid. A file
without "repos" or "history" loads with "" and [] for the new fields. Every page's data carries "link_origin" (LINK_ORIGIN).
The file's "changes" list (else []) and "changes_since" string (else "") are
read too: items that are not objects are dropped, missing or non-string
"repo", "commit", "author", "date", "subject" and "pr" become "", "files" that
is not a list becomes [], file items that are not objects or have a
non-string "path" are dropped, and a "status" other than added, modified or
removed becomes "modified". Each page's data carries "changes_since" and
"changes": only the changes whose "repo" is a repo label on that page, each
with "commit_url" and "pr_url" (built like the node URLs, "" when invalid),
and each file as {"path", "status", "id", "name"}, where "id" is "LABEL:path"
when a script node with that id is on the page (else "") and "name" is the
path's last segment. Each script node gets "added_at": the "date" of the
newest page change listing its path with status "added", else "". A file
without "changes" or "changes_since" loads with [], "" and "added_at" "".
On every page, a change file entry whose "id" is "" and whose "status" is not
"removed" is dropped (it is not a script on the page), and a change whose files
are all dropped this way is dropped too.

--ns-only LABEL (repeatable) marks the --repo with that LABEL as an app repo
that mixes SuiteScript with other JavaScript; a LABEL with no matching --repo
prints "error: --ns-only LABEL has no matching --repo" and exits 2 without
writing anything. In such a repo the walk also skips every directory named
node_modules, .git, .netlify, dist, build, coverage or vendor at any depth, and
a *.js file is a script only if its first 4,000 characters contain
"@NApiVersion" or "@NScriptType" (case-sensitive). Every other file is ignored
entirely: it gets no node, records, risk or summary, is not listed in
"skipped" and is not counted anywhere. In that repo's changes, file entries
with an "id" of "" are dropped whatever their status (a removed file cannot be
confirmed as SuiteScript), and a change left with no files is dropped. Repos
without --ns-only are scanned exactly as before.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

TEMPLATE = Path(__file__).resolve().parent / "script_map_template.html"
DATA_TOKEN = "__SCRIPT_MAP_DATA__"
MAX_BYTES = 2_000_000

EXCLUDED_DIRS = {
    ".git", "node_modules", "backups", "backup", "temp-handoffs",
    "tests", "test", "sandbox",
}
NS_ONLY_EXCLUDED_DIRS = {
    "node_modules", ".git", ".netlify", "dist", "build", "coverage", "vendor",
}
NS_ONLY_HEAD_CHARS = 4000
NS_ONLY_HEAD_BYTES = 16000
NS_ONLY_TAGS = ("@NApiVersion", "@NScriptType")

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

RECORD_CATEGORIES = [
    "Sales", "Billing / AR", "Purchasing / AP", "Items",
    "Inventory / Manufacturing", "Entities", "Accounting", "Custom records", "Other",
]

RECORD_AREAS = {
    record_id: area
    for area, ids in (
        ("Sales", ("estimate", "opportunity", "salesorder", "cashsale", "cashrefund",
                   "returnauthorization", "itemfulfillment")),
        ("Billing / AR", ("invoice", "customerpayment", "customerdeposit",
                          "depositapplication", "creditmemo", "customerrefund",
                          "statementcharge")),
        ("Purchasing / AP", ("purchaseorder", "purchaserequisition", "requisition",
                             "vendorbill", "vendorpayment", "vendorcredit",
                             "vendorprepayment", "vendorreturnauthorization",
                             "itemreceipt")),
        ("Inventory / Manufacturing", ("workorder", "workorderissue",
                                       "workordercompletion", "workorderclose",
                                       "assemblybuild", "assemblyunbuild",
                                       "inventoryadjustment", "inventorytransfer",
                                       "inventorycount", "inventorystatuschange",
                                       "inventorynumber", "transferorder", "bin",
                                       "bintransfer", "binworksheet")),
        ("Entities", ("customer", "vendor", "employee", "contact", "partner", "lead",
                      "prospect")),
        ("Accounting", ("journalentry", "intercompanyjournalentry",
                        "advintercompanyjournalentry", "account", "accountingperiod",
                        "currency", "deposit", "check", "transfer", "subsidiary",
                        "department", "location", "classification")),
    )
    for record_id in ids
}

RECORD_LABELS = {
    "estimate": "Estimate",
    "opportunity": "Opportunity",
    "salesorder": "Sales Order",
    "cashsale": "Cash Sale",
    "cashrefund": "Cash Refund",
    "returnauthorization": "Return Authorization",
    "itemfulfillment": "Item Fulfillment",
    "invoice": "Invoice",
    "customerpayment": "Customer Payment",
    "customerdeposit": "Customer Deposit",
    "depositapplication": "Deposit Application",
    "creditmemo": "Credit Memo",
    "customerrefund": "Customer Refund",
    "statementcharge": "Statement Charge",
    "purchaseorder": "Purchase Order",
    "purchaserequisition": "Purchase Requisition",
    "requisition": "Requisition",
    "vendorbill": "Vendor Bill",
    "vendorpayment": "Vendor Payment",
    "vendorcredit": "Vendor Credit",
    "vendorprepayment": "Vendor Prepayment",
    "vendorreturnauthorization": "Vendor Return Authorization",
    "itemreceipt": "Item Receipt",
    "workorder": "Work Order",
    "workorderissue": "Work Order Issue",
    "workordercompletion": "Work Order Completion",
    "workorderclose": "Work Order Close",
    "assemblybuild": "Assembly Build",
    "assemblyunbuild": "Assembly Unbuild",
    "inventoryadjustment": "Inventory Adjustment",
    "inventorytransfer": "Inventory Transfer",
    "inventorycount": "Inventory Count",
    "inventorystatuschange": "Inventory Status Change",
    "inventorynumber": "Inventory Number",
    "transferorder": "Transfer Order",
    "bin": "Bin",
    "bintransfer": "Bin Transfer",
    "binworksheet": "Bin Worksheet",
    "customer": "Customer",
    "vendor": "Vendor",
    "employee": "Employee",
    "contact": "Contact",
    "partner": "Partner",
    "lead": "Lead",
    "prospect": "Prospect",
    "journalentry": "Journal Entry",
    "intercompanyjournalentry": "Intercompany Journal Entry",
    "advintercompanyjournalentry": "Advanced Intercompany Journal Entry",
    "account": "Account",
    "accountingperiod": "Accounting Period",
    "currency": "Currency",
    "deposit": "Deposit",
    "check": "Check",
    "transfer": "Transfer",
    "subsidiary": "Subsidiary",
    "department": "Department",
    "location": "Location",
    "classification": "Class",
    "item": "Item",
    "inventoryitem": "Inventory Item",
    "noninventoryitem": "Non-Inventory Item",
    "serviceitem": "Service Item",
    "otherchargeitem": "Other Charge Item",
    "assemblyitem": "Assembly Item",
    "kititem": "Kit/Package",
    "lotnumberedinventoryitem": "Lot Numbered Inventory Item",
    "serializedinventoryitem": "Serialized Inventory Item",
    "lotnumberedassemblyitem": "Lot Numbered Assembly Item",
    "serializedassemblyitem": "Serialized Assembly Item",
    "discountitem": "Discount Item",
    "markupitem": "Markup Item",
    "paymentitem": "Payment Item",
    "subtotalitem": "Subtotal Item",
    "descriptionitem": "Description Item",
    "giftcertificateitem": "Gift Certificate Item",
    "job": "Project",
    "supportcase": "Case",
    "task": "Task",
    "phonecall": "Phone Call",
    "calendarevent": "Event",
    "folder": "Folder",
}

TYPE_BLOCKLIST = {
    "text", "textarea", "select", "multiselect", "checkbox", "date",
    "datetime", "datetimetz", "timeofday", "currency", "float", "integer",
    "percent", "phone", "email", "url", "inlinehtml", "longtext", "richtext",
    "label", "help", "hidden", "file", "image", "password", "submit",
    "button", "list", "inline", "normal", "entry", "staticlist", "editor",
    "plain", "radio", "inlineeditor",
}

FOLDER_TYPES = [
    ("userevent", "UserEventScript"),
    ("mapreduce", "MapReduceScript"),
    ("massupdate", "MassUpdateScript"),
    ("workflow", "WorkflowActionScript"),
    ("scheduled", "ScheduledScript"),
    ("suitelet", "Suitelet"),
    ("restlet", "Restlet"),
    ("portlet", "Portlet"),
    ("client", "ClientScript"),
]

VERSION_BUCKETS = ("1.0", "2.0", "2.x", "2.1")

SCRIPT_TYPE_RE = re.compile(r"@NScriptType\s+(\w+)")
API_VERSION_RE = re.compile(r"@NApiVersion\s+([^\s*]+)")
RETURN_RE = re.compile(r"\breturn\s*\{")
KEY_RE = re.compile(r"(?:^|[{,])\s*([A-Za-z_$][\w$]*)\s*(?=[,:}]|$)")
DEPLOY_RE = re.compile(r"DEPLOY ON:(.*)", re.IGNORECASE)
ID_RE = re.compile(r"[A-Za-z0-9_]+")
PAREN_RE = re.compile(r"\(([^)]*)\)")
DEPLOY_NAME_RE = re.compile(r"[A-Za-z0-9_]+(?:[ \t]+[A-Za-z0-9_]+)*")
DEPLOY_SEP_RE = re.compile(r"[,;/&]|\band\b", re.IGNORECASE)
TYPE_ENUM_RE = re.compile(r"\b(?:record|search)\.Type\.([A-Za-z0-9_]+)")
TYPE_LITERAL_RE = re.compile(r"\btype\s*:\s*(['\"])([^'\"\n]*)\1")
RECORD_ID_RE = re.compile(r"[a-z][a-z0-9_]*")
CUSTOMRECORD_RE = re.compile(r"(['\"])(customrecord[a-z0-9_]*)\1", re.IGNORECASE)
RECORD_CALL_RE = re.compile(
    r"\b[A-Za-z_$][\w$]*\.(?:create|load|copy|transform|submitFields|delete|attach|"
    r"detach|lookupFields|newSearch)\s*\(")
CALL_SPAN_LIMIT = 2000
DEFINE_RE = re.compile(r"\bdefine\s*\(\s*\[")
STRING_RE = re.compile(r"['\"]([^'\"]+)['\"]")
CLIENT_PATH_RE = re.compile(r"\bclientScriptModulePath\s*=\s*(['\"])([^'\"]+)\1")
TRY_RE = re.compile(r"\btry\s*\{")
HARDCODED_ID_RE = re.compile(r"(?i)\b(?:id|internalid)\s*[:=]\s*['\"]?\d+\b")
NAMESPACE_RE = re.compile(r"[A-Za-z0-9_-]*")
SHA_RE = re.compile(r"[0-9a-fA-F]{40}([0-9a-fA-F]{24})?")
BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")
MAX_HEADER = 300
MAX_HISTORY = 5
HISTORY_FIELDS = ("summary", "generated_at", "trigger", "author", "guidance")
MAX_TEAM_NAME = 40
LINK_ORIGIN = "https://github.com/"
SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}")
PR_RE = re.compile(r"[0-9]{1,9}")
MAX_COMMITS = 5
COMMIT_FIELDS = ("commit", "author", "date", "subject", "pr")
CHANGE_FIELDS = ("repo",) + COMMIT_FIELDS
CHANGE_STATUSES = ("added", "modified", "removed")


def load_summary_cache(path):
    """Return the parsed summary cache at path.

    Raises OSError if the file cannot be read (FileNotFoundError when missing) and
    ValueError unless it is a JSON object with version == 1 and an "entries" object.
    """
    raw = Path(path).read_bytes()
    try:
        cache = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError(f"{path} is not a valid summary cache") from None
    if (not isinstance(cache, dict) or cache.get("version") != 1
            or not isinstance(cache.get("entries"), dict)):
        raise ValueError(f"{path} is not a valid summary cache")
    return cache


def load_git_info(path):
    """Return the parsed git info file at path.

    Raises OSError if the file cannot be read (FileNotFoundError when missing) and
    ValueError unless it is a JSON object with version == 1 and a "files" object.
    """
    raw = Path(path).read_bytes()
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError(f"{path} is not a valid git info file") from None
    if (not isinstance(info, dict) or info.get("version") != 1
            or not isinstance(info.get("files"), dict)):
        raise ValueError(f"{path} is not a valid git info file")
    return info


def history_item(value):
    """Return {summary, generated_at, trigger, author, guidance} from a cache entry or
    history item; non-string or missing fields become ""."""
    return {field: value.get(field) if isinstance(value.get(field), str) else ""
            for field in HISTORY_FIELDS}


def normalize_history(value):
    """Return a cache entry's "history" as at most MAX_HISTORY normalized items.

    A value that is not a list gives []; items that are not objects are dropped.
    """
    if not isinstance(value, list):
        return []
    return [history_item(item) for item in value if isinstance(item, dict)][:MAX_HISTORY]


def commit_url(slug, commit):
    """Return the GitHub commit page URL, or "" unless slug and commit are valid."""
    if not (SLUG_RE.fullmatch(slug) and COMMIT_RE.fullmatch(commit)):
        return ""
    return f"{LINK_ORIGIN}{slug}/commit/{commit}"


def pr_url(slug, pr):
    """Return the GitHub pull request URL, or "" unless slug and pr are valid."""
    if not (SLUG_RE.fullmatch(slug) and PR_RE.fullmatch(pr)):
        return ""
    return f"{LINK_ORIGIN}{slug}/pull/{pr}"


def file_url(slug, commit, path, kind):
    """Return the GitHub "blob" or "raw" URL of path at commit, or "" unless slug and
    commit are valid. The path is percent-encoded, keeping "/"."""
    if not (SLUG_RE.fullmatch(slug) and COMMIT_RE.fullmatch(commit)):
        return ""
    return f"{LINK_ORIGIN}{slug}/{kind}/{commit}/{quote(path, safe='/')}"


def commit_item(value, slug, path):
    """Return {commit, author, date, subject, pr} from a git info history item plus its
    commit_url, pr_url, file_url and raw_url; non-string or missing fields become ""."""
    item = {field: value.get(field) if isinstance(value.get(field), str) else ""
            for field in COMMIT_FIELDS}
    item["commit_url"] = commit_url(slug, item["commit"])
    item["pr_url"] = pr_url(slug, item["pr"])
    item["file_url"] = file_url(slug, item["commit"], path, "blob")
    item["raw_url"] = file_url(slug, item["commit"], path, "raw")
    return item


def normalize_commit_history(value, slug, path, current=""):
    """Return a git info entry's "history" as at most MAX_COMMITS previous commit items.

    A value that is not a list gives []; items that are not objects are dropped, and
    so is every item whose "commit" equals current when current is non-empty.
    """
    if not isinstance(value, list):
        return []
    items = [item for item in value if isinstance(item, dict)
             and not (current and item.get("commit") == current)][:MAX_COMMITS]
    return [commit_item(item, slug, path) for item in items]


def normalize_changes(value):
    """Return a git info file's "changes" as a list of {repo, commit, author, date,
    subject, pr, files: [{path, status}]}.

    A value that is not a list gives []; items that are not objects are dropped and
    missing or non-string fields become "". "files" that is not a list gives [];
    file items that are not objects or whose "path" is not a string are dropped, and
    a "status" outside CHANGE_STATUSES becomes "modified".
    """
    if not isinstance(value, list):
        return []
    changes = []
    for item in value:
        if not isinstance(item, dict):
            continue
        change = {field: item.get(field) if isinstance(item.get(field), str) else ""
                  for field in CHANGE_FIELDS}
        files = item.get("files") if isinstance(item.get("files"), list) else []
        change["files"] = [
            {"path": entry["path"],
             "status": entry.get("status") if entry.get("status") in CHANGE_STATUSES
             else "modified"}
            for entry in files
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)]
        changes.append(change)
    return changes


def header_description(text):
    """Return a plain-text description from the first /** ... */ block, or "".

    Each line is stripped of leading whitespace and "*". An @description tag wins
    (its text plus following lines up to the next @ line); otherwise the lines
    before the first @ line are used. Empty and "DEPLOY ON:" lines are dropped and
    whitespace is collapsed. A result over 300 characters becomes its first 300
    characters followed by "…" (301 characters in total).
    """
    start = text.find("/**")
    if start == -1:
        return ""
    end = text.find("*/", start + 3)
    if end == -1:
        return ""
    lines = [line.lstrip().lstrip("*").strip() for line in text[start + 3:end].split("\n")]
    picked = []
    tagged = next((i for i, line in enumerate(lines) if line.startswith("@description")), None)
    if tagged is not None:
        picked.append(lines[tagged][len("@description"):])
        for line in lines[tagged + 1:]:
            if line.startswith("@"):
                break
            picked.append(line)
    else:
        for line in lines:
            if line.startswith("@"):
                break
            picked.append(line)
    kept = [line for line in picked if line.strip() and "deploy on:" not in line.lower()]
    result = " ".join(" ".join(kept).split())
    if len(result) > MAX_HEADER:
        result = result[:MAX_HEADER] + "…"
    return result


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


def branch_status(path, expected="main"):
    """Return expected, None (not verified), or the offending branch name.

    expected is the branch PATH/.git/HEAD must name ("ref: refs/heads/<expected>").
    Raises OSError if PATH/.git is a directory but its HEAD cannot be read.
    """
    git = Path(path) / ".git"
    if not git.is_dir():
        return None
    head = (git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    if head == f"ref: refs/heads/{expected}":
        return expected
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


def infer_type_from_path(rel_path):
    """Return the script type named by the deepest matching folder of rel_path, or None.

    Only directory segments are checked (never the file name); each is reduced to
    lowercase letters and matched against the FOLDER_TYPES keywords in order.
    """
    for segment in reversed(rel_path.split("/")[:-1]):
        normalized = re.sub(r"[^a-z]", "", segment.lower())
        for keyword, script_type in FOLDER_TYPES:
            if keyword in normalized:
                return script_type
    return None


def version_bucket(api_version):
    """Return "1.0", "2.0", "2.x", "2.1", "None" (empty) or "Other" for an API version."""
    value = (api_version or "").strip().lower()
    if value in VERSION_BUCKETS:
        return value
    if not value:
        return "None"
    return "Other"


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
    top = []
    depth = 0
    for char in text[start:end + 1]:
        if char in "{[(":
            depth += 1
            if depth == 1:
                top.append(char)
        elif char in "}])":
            if depth == 1:
                top.append(char)
            depth -= 1
        elif depth == 1:
            top.append(char)
    body = "".join(top)
    allowed = set(ENTRY_POINTS)
    found = []
    for match in KEY_RE.finditer(body):
        key = match.group(1)
        if key in allowed and key not in found:
            found.append(key)
    return found


def _split_deploy_parts(text):
    """Split text at DEPLOY_SEP_RE matches outside parentheses; return stripped, non-empty parts."""
    parts = []
    depth = 0
    start = 0
    i = 0
    while i < len(text):
        char = text[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        elif depth == 0:
            sep = DEPLOY_SEP_RE.match(text, i)
            if sep:
                parts.append(text[start:i])
                i = start = sep.end()
                continue
        i += 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def deploy_targets(text):
    """Return [(record_id, label), ...] from every `DEPLOY ON:` line, deduplicated.

    Each line is split on , ; / & and the word "and" outside parentheses; each part's name is
    the first space-separated word run before any "(" (record_id: joined and lowercased), and
    its label is the parenthesized text, else the name as written if it has spaces or capitals,
    else record_id.
    """
    targets = []
    seen = set()
    for line in text.split("\n"):
        match = DEPLOY_RE.search(line)
        if not match:
            continue
        for part in _split_deploy_parts(match.group(1)):
            run = DEPLOY_NAME_RE.search(part.split("(", 1)[0])
            if not run:
                continue
            name = run.group(0)
            record_id = re.sub(r"\s+", "", name).lower()
            paren = PAREN_RE.search(part)
            if paren and paren.group(1).strip():
                label = paren.group(1).strip()
            elif re.search(r"\s|[A-Z]", name):
                label = " ".join(name.split())
            else:
                label = record_id
            if record_id not in seen:
                seen.add(record_id)
                targets.append((record_id, label))
    return targets


def record_call_spans(text):
    """Return [(start, end), ...] for the parenthesized arguments of each record call.

    A record call is IDENT.METHOD( matching RECORD_CALL_RE. start is the "(" index and
    end the index of its matching ")"; quoted characters (', ", `, with backslash
    escapes) are skipped. Without a match within CALL_SPAN_LIMIT characters the span
    ends at start + CALL_SPAN_LIMIT.
    """
    spans = []
    for match in RECORD_CALL_RE.finditer(text):
        start = match.end() - 1
        limit = min(len(text), start + CALL_SPAN_LIMIT + 1)
        end = start + CALL_SPAN_LIMIT
        depth = 0
        quote_char = None
        i = start
        while i < limit:
            char = text[i]
            if quote_char:
                if char == "\\":
                    i += 1
                elif char == quote_char:
                    quote_char = None
            elif char in "'\"`":
                quote_char = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
            i += 1
        spans.append((start, end))
    return spans


def known_record_id(value):
    """Return True if value is a RECORD_AREAS id, "item", an *item id, or starts with
    customrecord or customtransaction."""
    return (value in RECORD_AREAS or value.endswith("item")
            or value.startswith(("customrecord", "customtransaction")))


def touched_records(text):
    """Return record ids referenced by record/search.Type, type: literals and customrecord strings.

    record/search.Type.X and quoted customrecord* strings always count. A `type: '...'`
    literal counts only if it is a lowercase id outside TYPE_BLOCKLIST and either is a
    known record id (known_record_id) or lies inside a record call's arguments
    (record_call_spans).
    """
    found = []

    def add(record_id):
        if record_id not in found:
            found.append(record_id)

    for match in TYPE_ENUM_RE.finditer(text):
        add(match.group(1).lower().replace("_", ""))
    spans = None
    for match in TYPE_LITERAL_RE.finditer(text):
        value = match.group(2)
        if not RECORD_ID_RE.fullmatch(value) or value in TYPE_BLOCKLIST:
            continue
        if known_record_id(value):
            add(value)
            continue
        if spans is None:
            spans = record_call_spans(text)
        if any(start < match.start() < end for start, end in spans):
            add(value)
    for match in CUSTOMRECORD_RE.finditer(text):
        add(match.group(2).lower())
    return found


def record_category(record_id):
    """Return the business area (one of RECORD_CATEGORIES) for a lowercase record id.

    Rules in order: customrecord* ids are "Custom records"; ids in RECORD_AREAS
    take their listed area; "item" or any id ending in "item" is "Items"; anything
    else is "Other".
    """
    if record_id.startswith("customrecord"):
        return "Custom records"
    if record_id in RECORD_AREAS:
        return RECORD_AREAS[record_id]
    if record_id == "item" or record_id.endswith("item"):
        return "Items"
    return "Other"


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


def js_files(root, errors=None, extra_excluded=()):
    """Yield (relative_posix_path, absolute_path) for candidate *.js files.

    OSErrors from listing directories are appended to errors when it is a list.
    Directory names in extra_excluded are skipped as well as EXCLUDED_DIRS.
    """
    onerror = errors.append if errors is not None else None
    excluded = EXCLUDED_DIRS | set(extra_excluded)
    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in excluded)
        for name in sorted(filenames):
            lower = name.lower()
            if not lower.endswith(".js") or lower.endswith(".min.js"):
                continue
            full = Path(dirpath) / name
            yield full.relative_to(root).as_posix(), full


def collect(repos, summaries=None, branch="main", summary_namespace="", git_info=None,
            git_repos=None, git_changes=None, changes_since="", ns_only=None):
    """Return the JSON-serializable map data for [(label, Path), ...].

    summaries is the "entries" dict of a summary cache (see load_summary_cache).
    branch is the branch each repo's HEAD is expected to name.
    summary_namespace, when non-empty, prefixes each script's summary_key with "NAME|".
    git_info is the "files" dict of a git info file (see load_git_info) and
    git_repos its "repos" dict of LABEL -> GitHub "OWNER/REPO" slug.
    git_changes is its "changes" list (see normalize_changes) and changes_since
    its "changes_since" string.
    ns_only is the set of labels whose repos count only files tagged in their first
    NS_ONLY_HEAD_CHARS characters (see the module docstring); other files there are
    ignored without a "skipped" entry.
    """
    ns_only = set(ns_only or ())
    summaries = summaries or {}
    git_info = git_info or {}
    git_repos = git_repos or {}
    git_changes = git_changes or []
    key_prefix = f"{summary_namespace}|" if summary_namespace else ""
    by_sha = {}
    for key in sorted(summaries):
        value = summaries[key]
        if (isinstance(value, dict) and isinstance(value.get("sha256"), str)
                and isinstance(value.get("summary"), str)):
            by_sha.setdefault(value["sha256"], value)
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
        walk_errors = []
        app_repo = label in ns_only
        extra = NS_ONLY_EXCLUDED_DIRS if app_repo else ()
        for rel, full in js_files(root, walk_errors, extra):
            try:
                if app_repo:
                    with open(full, "rb") as handle:
                        head = handle.read(NS_ONLY_HEAD_BYTES)
                    head = head.decode("utf-8", errors="replace")[:NS_ONLY_HEAD_CHARS]
                    if not any(tag in head for tag in NS_ONLY_TAGS):
                        continue
                if os.path.getsize(full) > MAX_BYTES:
                    skipped.append({"repo": label, "path": rel, "reason": "too large"})
                    continue
                blob = full.read_bytes()
            except OSError as exc:
                skipped.append({"repo": label, "path": rel,
                                "reason": f"unreadable: {exc.strerror}"})
                continue
            sha = hashlib.sha256(blob).hexdigest()
            raw = blob.decode("utf-8", errors="replace")
            text = raw.replace("\r\n", "\n").replace("\r", "\n")
            is_script, script_type, api_version = classify(text)
            if not is_script:
                skipped.append({"repo": label, "path": rel, "reason": "not SuiteScript"})
                continue
            scripts.append((rel, text, script_type, api_version, sha))
        for exc in walk_errors:
            rel = Path(exc.filename).relative_to(root).as_posix()
            skipped.append({"repo": label, "path": rel,
                            "reason": f"unreadable: {exc.strerror}"})
        scripts.sort(key=lambda item: item[0])

        by_basename = {}
        for rel, _, _, _, _ in scripts:
            by_basename.setdefault(basename_key(rel), []).append(f"{label}:{rel}")

        records = {}
        script_nodes = []
        for rel, text, script_type, api_version, sha in scripts:
            node_id = f"{label}:{rel}"
            cached = summaries.get(node_id)
            if not (isinstance(cached, dict) and cached.get("sha256") == sha
                    and isinstance(cached.get("summary"), str)):
                cached = by_sha.get(sha)
            summary_generated_at = ""
            summary_trigger = summary_author = summary_guidance = ""
            summary_history = []
            if cached is not None:
                summary, summary_source = cached["summary"], "ai"
                if isinstance(cached.get("generated_at"), str):
                    summary_generated_at = cached["generated_at"]
                if summary:
                    details = history_item(cached)
                    summary_trigger = details["trigger"]
                    summary_author = details["author"]
                    summary_guidance = details["guidance"]
                    summary_history = normalize_history(cached.get("history"))
            else:
                summary = header_description(text)
                summary_source = "header" if summary else ""
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

            last = git_info.get(node_id)
            if not isinstance(last, dict):
                last = {}
            last_commit, last_author, last_date = (
                last.get(field) if isinstance(last.get(field), str) else ""
                for field in ("commit", "author", "date"))
            last_subject, last_pr = (
                last.get(field) if isinstance(last.get(field), str) else ""
                for field in ("subject", "pr"))
            slug = git_repos.get(label)
            if not isinstance(slug, str):
                slug = ""

            type_inferred = False
            if script_type == "SuiteScript 1.0":
                inferred = infer_type_from_path(rel)
                type_inferred = inferred is not None
                script_type = inferred or "Unknown"

            script_nodes.append({
                "id": node_id,
                "kind": "script",
                "repo": label,
                "path": rel,
                "name": rel.rsplit("/", 1)[-1],
                "type": script_type,
                "type_inferred": type_inferred,
                "api_version": api_version,
                "version": version_bucket(api_version),
                "entry_points": entry_points(text),
                "modules": modules,
                "has_deploy_line": bool(DEPLOY_RE.search(text)),
                "risk": risk(text, len(set(deploy_ids) | set(touches))),
                "summary": summary,
                "summary_source": summary_source,
                "summary_generated_at": summary_generated_at,
                "summary_trigger": summary_trigger,
                "summary_author": summary_author,
                "summary_guidance": summary_guidance,
                "summary_history": summary_history,
                "summary_key": f"{key_prefix}{node_id}",
                "last_commit": last_commit,
                "last_commit_author": last_author,
                "last_commit_date": last_date,
                "last_commit_subject": last_subject,
                "last_commit_pr": last_pr,
                "last_commit_url": commit_url(slug, last_commit),
                "last_commit_pr_url": pr_url(slug, last_pr),
                "last_commit_file_url": file_url(slug, last_commit, rel, "blob"),
                "last_commit_raw_url": file_url(slug, last_commit, rel, "raw"),
                "commit_history": normalize_commit_history(
                    last.get("history"), slug, rel, last_commit),
                "added_at": "",
            })

        nodes.extend(script_nodes)
        for record_id in sorted(records):
            nodes.append({
                "id": f"{label}:{record_id}",
                "kind": "record",
                "repo": label,
                "record_id": record_id,
                "label": records[record_id] or RECORD_LABELS.get(record_id, record_id),
                "category": record_category(record_id),
            })
        status = branch_status(root, branch)
        repo_rows.append({
            "label": label,
            "branch": branch if status == branch else "branch not verified",
            "script_count": len(script_nodes),
        })

    page_labels = {label for label, _ in repos}
    scripts_by_id = {node["id"]: node for node in nodes if node["kind"] == "script"}
    changes = []
    for change in git_changes:
        label = change["repo"]
        if label not in page_labels:
            continue
        slug = git_repos.get(label)
        if not isinstance(slug, str):
            slug = ""
        files = []
        for entry in change["files"]:
            path = entry["path"]
            node = scripts_by_id.get(f"{label}:{path}")
            if not node and (entry["status"] != "removed" or label in ns_only):
                continue
            files.append({"path": path, "status": entry["status"],
                          "id": node["id"] if node else "",
                          "name": path.rsplit("/", 1)[-1]})
            if node and entry["status"] == "added" and change["date"] > node["added_at"]:
                node["added_at"] = change["date"]
        if change["files"] and not files:
            continue
        changes.append(dict(change, files=files,
                            commit_url=commit_url(slug, change["commit"]),
                            pr_url=pr_url(slug, change["pr"])))

    return {"repos": repo_rows, "nodes": nodes, "edges": edges, "skipped": skipped,
            "record_categories": list(RECORD_CATEGORIES),
            "changes_since": changes_since, "changes": changes}


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
    parser.add_argument("--summaries", default=None, metavar="FILE",
                        help="summary cache written by script_map_summarize.py")
    parser.add_argument("--refresh-url", default=None, metavar="PATH",
                        help="site-relative path for the Refresh data button, e.g. /api/rebuild")
    parser.add_argument("--branch", default="main", metavar="NAME",
                        help="branch every repo's HEAD must name (default main)")
    parser.add_argument("--environment", default="Production", metavar="NAME",
                        help="environment shown in the header badge (default Production)")
    parser.add_argument("--switch-url", default=None, metavar="PATH",
                        help="site-relative path of the other environment's page, e.g. "
                             "/sandbox.html")
    parser.add_argument("--switch-label", default=None, metavar="TEXT",
                        help="toggle label for --switch-url, e.g. Sandbox")
    parser.add_argument("--missing-repo", action="append", default=None, metavar="LABEL",
                        help="repo left out because it has no --branch branch; repeatable")
    parser.add_argument("--summary-namespace", default="", metavar="NAME",
                        help="script_map_summarize.py --namespace of this page's summaries; "
                             "prefixes each script's summary_key with NAME| (default none)")
    parser.add_argument("--team", action="append", default=None, metavar="NAME",
                        help="MBSG team member name listed in the page's team field "
                             f"(1-{MAX_TEAM_NAME} characters); repeatable")
    parser.add_argument("--git-info", default=None, metavar="FILE",
                        help="last-change file written by script_map_gitinfo.py")
    parser.add_argument("--ns-only", action="append", default=None, metavar="LABEL",
                        help="--repo LABEL whose scripts must carry @NApiVersion or "
                             "@NScriptType in their first 4,000 characters; other files "
                             "and build folders are ignored; repeatable")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    def site_relative(value):
        return value.startswith("/") and not any(c.isspace() for c in value)

    if args.refresh_url is not None and not site_relative(args.refresh_url):
        print("error: --refresh-url must be a site-relative path starting with /",
              file=sys.stderr)
        return 2
    if not BRANCH_RE.fullmatch(args.branch):
        print("error: --branch must be a branch name", file=sys.stderr)
        return 2
    if not args.environment.strip():
        print("error: --environment must not be empty", file=sys.stderr)
        return 2
    if args.switch_url is not None and not site_relative(args.switch_url):
        print("error: --switch-url must be a site-relative path starting with /",
              file=sys.stderr)
        return 2
    if args.switch_url is not None and not args.switch_label:
        print("error: --switch-label is required with --switch-url", file=sys.stderr)
        return 2
    if not NAMESPACE_RE.fullmatch(args.summary_namespace):
        print("error: --summary-namespace must be letters, digits, _ or -", file=sys.stderr)
        return 2
    team = []
    for value in args.team or []:
        name = value.strip()
        if (not 1 <= len(name) <= MAX_TEAM_NAME
                or any(unicodedata.category(c) == "Cc" for c in name)):
            print(f"error: --team must be 1-{MAX_TEAM_NAME} characters", file=sys.stderr)
            return 2
        team.append(name)

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
    ns_only = set(args.ns_only or [])
    for label in args.ns_only or []:
        if label not in labels:
            print(f"error: --ns-only {label} has no matching --repo", file=sys.stderr)
            return 2

    warnings = []
    for label, path in repos:
        try:
            status = branch_status(path, args.branch)
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

    entries = None
    if args.summaries is not None:
        try:
            entries = load_summary_cache(args.summaries)["entries"]
        except FileNotFoundError:
            print(f"error: {args.summaries} does not exist", file=sys.stderr)
            return 2
        except (OSError, ValueError):
            print(f"error: {args.summaries} is not a valid summary cache", file=sys.stderr)
            return 2

    git_files = None
    git_repos = None
    git_changes = []
    changes_since = ""
    if args.git_info is not None:
        try:
            info = load_git_info(args.git_info)
            git_files = info["files"]
            git_repos = info.get("repos") if isinstance(info.get("repos"), dict) else {}
            git_changes = normalize_changes(info.get("changes"))
            if isinstance(info.get("changes_since"), str):
                changes_since = info["changes_since"]
        except FileNotFoundError:
            print(f"error: {args.git_info} does not exist", file=sys.stderr)
            return 2
        except (OSError, ValueError):
            print(f"error: {args.git_info} is not a valid git info file", file=sys.stderr)
            return 2

    try:
        data = collect(repos, entries, args.branch, args.summary_namespace,
                       git_info=git_files, git_repos=git_repos,
                       git_changes=git_changes, changes_since=changes_since,
                       ns_only=ns_only)
    except OSError as exc:
        for label, path in repos:
            head = path / ".git" / "HEAD"
            if exc.filename == str(head):
                print(f"error: {label} cannot read {head}: {exc.strerror}", file=sys.stderr)
                break
        else:
            print(f"error: cannot read {exc.filename}: {exc.strerror}", file=sys.stderr)
        return 2
    data["title"] = args.title
    data["refresh_url"] = args.refresh_url or ""
    data["environment"] = args.environment
    data["switch_url"] = args.switch_url or ""
    data["switch_label"] = args.switch_label or ""
    data["missing_repos"] = args.missing_repo or []
    data["branch"] = args.branch
    data["team"] = sorted(set(team), key=lambda name: (name.casefold(), name))
    data["link_origin"] = LINK_ORIGIN
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
