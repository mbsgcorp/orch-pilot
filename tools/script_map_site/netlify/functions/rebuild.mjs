// Netlify Function: record a "rebuild requested" file on a branch of the site repo.
// Hermes polls that branch and rebuilds the script map; requests are limited to one
// per 10 minutes. Uses only Node built-ins (fetch, Response, Buffer, crypto).
//
// A POST whose body is {"key": "[NS|]LABEL:path"} instead records a per-script
// "regenerate summary" request in RESUMMARIZE_PATH on the same branch, as
// {"version": 1, "requests": {"<key>": "<iso>"}}, limited to one per key per 10
// minutes. Entries older than 7 days are dropped and at most 500 are kept.
//
// Correction notes live in NOTES_PATH (default summary-notes.json) on the same
// branch, as {"version": 1, "notes": {"LABEL:path": [{"id", "text", "author",
// "created_at", "cleared_at", "cleared_by", "clear_reason"}, ...]}}. The key's
// "NS|" prefix is stripped, so prod and sandbox share notes; a note is active
// while cleared_at is null.
//   {"action": "add_note", "key", "text", "author"} appends a note (400 on bad
//   input, 409 too_many_notes when 10 are already active, 413 notes_full when a
//   new script would exceed 2000 scripts).
//   {"action": "clear_note", "key", "note_id", "author", "reason"} clears one
//   (404 not_found, 409 already_cleared).
// Each script keeps its active notes plus its 40 most recently cleared ones. A
// write rejected with 409/422 is re-read, re-applied and retried once. After a
// save, a per-key regenerate request is queued as for {"key"} and the reply is
// 202 {"status": "saved", "note", "regenerate": "requested" | "already_requested"
// | "failed", "requested_at"}. Any other "action" is 400.
//
// A GET reads the three files (no writes) and returns {"status": "ok",
// "rebuild_requested_at": <iso or null>, "requests": {"<key>": "<iso>"},
// "notes": {"LABEL:path": [note, ...]}} with cache-control: no-store, so the page
// can show which requests are still pending and the notes for each script.

export const config = { path: "/api/rebuild" };

const MIN_INTERVAL_MS = 10 * 60 * 1000;
const MAX_REQUEST_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const MAX_REQUESTS = 500;
const KEY_RE = /^(?:[A-Za-z0-9_-]+\|)?[^|:\r\n]{1,100}:[^\r\n]{1,300}$/;
const MAX_ACTIVE_NOTES = 10;
const MAX_CLEARED_NOTES = 40;
const MAX_NOTE_IDS = 2000;
const NOTE_FIELDS = ["id", "text", "author", "created_at"];
const NOTE_CLEARED_FIELDS = ["cleared_at", "cleared_by", "clear_reason"];

function json(status, obj, extraHeaders = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", ...extraHeaders },
  });
}

function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function githubHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "mbsg-script-map",
  };
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function redact(err, token) {
  return String((err && err.message) || err).split(token).join("***");
}

export default async (req) => {
  if (req.method === "GET") {
    return readStatus();
  }
  if (req.method !== "POST") {
    return json(405, { status: "method_not_allowed" });
  }

  const token = process.env.GITHUB_TOKEN;
  const repo = process.env.REBUILD_REPO;
  const branch = process.env.REBUILD_BRANCH || "rebuild-requests";
  const path = process.env.REBUILD_PATH || "rebuild-request.json";
  if (!token || !repo) {
    return json(500, { status: "error", error: "not configured" });
  }

  const headers = githubHeaders(token);
  const url = `https://api.github.com/repos/${repo}/contents/${path}`;

  let text;
  try {
    text = await req.text();
  } catch {
    return json(400, { status: "bad_request" });
  }
  if (text !== "") {
    let parsed = null;
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
    if (isPlainObject(parsed) && Object.prototype.hasOwnProperty.call(parsed, "action")) {
      if (parsed.action === "add_note" || parsed.action === "clear_note") {
        return handleNote(parsed, { token, repo, branch, headers });
      }
      return json(400, { status: "bad_request" });
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed) ||
        typeof parsed.key !== "string" || !KEY_RE.test(parsed.key)) {
      return json(400, { status: "bad_request" });
    }
    return requestResummarize(parsed.key, {
      token,
      branch,
      headers,
      url: resummarizeUrl(repo),
    });
  }

  try {
    const getRes = await fetch(url + "?ref=" + encodeURIComponent(branch), { method: "GET", headers });
    let sha = null;
    if (getRes.status === 200) {
      const body = await getRes.json();
      sha = body.sha;
      let prior = null;
      try {
        prior = JSON.parse(Buffer.from(body.content, "base64").toString("utf8"));
      } catch {
        prior = null;
      }
      const requestedAt = prior && typeof prior.requested_at === "string" ? prior.requested_at : null;
      const t = requestedAt === null ? NaN : Date.parse(requestedAt);
      if (!Number.isNaN(t) && Date.now() - t < MIN_INTERVAL_MS) {
        return json(429, { status: "too_soon", requested_at: requestedAt });
      }
    } else if (getRes.status !== 404) {
      return json(502, { status: "error", error: "github " + getRes.status });
    }

    const iso = nowIso();
    const content = Buffer.from(
      JSON.stringify({ requested_at: iso, id: crypto.randomUUID() }),
      "utf8",
    ).toString("base64");
    const putBody = { message: "rebuild requested " + iso, content, branch };
    if (sha) {
      putBody.sha = sha;
    }
    const putRes = await fetch(url, {
      method: "PUT",
      headers: { ...headers, "content-type": "application/json" },
      body: JSON.stringify(putBody),
    });
    if (putRes.status === 200 || putRes.status === 201) {
      return json(202, { status: "requested", requested_at: iso });
    }
    return json(502, { status: "error", error: "github " + putRes.status });
  } catch (err) {
    return json(502, { status: "error", error: "github " + redact(err, token) });
  }
};

function resummarizeUrl(repo) {
  const resummarizePath = process.env.RESUMMARIZE_PATH || "resummarize-requests.json";
  return `https://api.github.com/repos/${repo}/contents/${resummarizePath}`;
}

async function requestResummarize(key, ctx) {
  const outcome = await queueResummarize(key, ctx);
  if (outcome.kind === "requested") {
    return json(202, { status: "requested", key, requested_at: outcome.requested_at });
  }
  if (outcome.kind === "too_soon") {
    return json(429, { status: "too_soon", requested_at: outcome.requested_at });
  }
  return json(502, { status: "error", error: outcome.error });
}

// Returns { kind: "requested" | "too_soon", requested_at } or { kind: "error", error }.
async function queueResummarize(key, { token, branch, headers, url }) {
  try {
    const getRes = await fetch(url + "?ref=" + encodeURIComponent(branch), { method: "GET", headers });
    let sha = null;
    let requests = {};
    if (getRes.status === 200) {
      const body = await getRes.json();
      sha = body.sha;
      let prior = null;
      try {
        prior = JSON.parse(Buffer.from(body.content, "base64").toString("utf8"));
      } catch {
        prior = null;
      }
      const priorRequests = prior && typeof prior === "object" ? prior.requests : null;
      if (priorRequests && typeof priorRequests === "object" && !Array.isArray(priorRequests)) {
        requests = priorRequests;
      }
    } else if (getRes.status !== 404) {
      return { kind: "error", error: "github " + getRes.status };
    }

    const now = Date.now();
    const requestedAt = Object.prototype.hasOwnProperty.call(requests, key) &&
      typeof requests[key] === "string" ? requests[key] : null;
    const t = requestedAt === null ? NaN : Date.parse(requestedAt);
    if (!Number.isNaN(t) && now - t < MIN_INTERVAL_MS) {
      return { kind: "too_soon", requested_at: requestedAt };
    }

    const iso = nowIso();
    const kept = [[key, iso, Date.parse(iso)]];
    for (const k of Object.keys(requests)) {
      const value = requests[k];
      const when = typeof value === "string" ? Date.parse(value) : NaN;
      if (k !== key && !Number.isNaN(when) && now - when <= MAX_REQUEST_AGE_MS) {
        kept.push([k, value, when]);
      }
    }
    kept.sort((a, b) => b[2] - a[2]);
    const next = {};
    for (const [k, value] of kept.slice(0, MAX_REQUESTS)) {
      next[k] = value;
    }

    const content = Buffer.from(
      JSON.stringify({ version: 1, requests: next }),
      "utf8",
    ).toString("base64");
    const putBody = { message: "resummarize requested " + iso, content, branch };
    if (sha) {
      putBody.sha = sha;
    }
    const putRes = await fetch(url, {
      method: "PUT",
      headers: { ...headers, "content-type": "application/json" },
      body: JSON.stringify(putBody),
    });
    if (putRes.status === 200 || putRes.status === 201) {
      return { kind: "requested", requested_at: iso };
    }
    return { kind: "error", error: "github " + putRes.status };
  } catch (err) {
    return { kind: "error", error: "github " + redact(err, token) };
  }
}

// Notes ---------------------------------------------------------------------

// The notes-map id for a summary key: the key without a leading "NS|".
function notesId(key) {
  const head = key.split(":", 1)[0];
  return head.includes("|") ? key.slice(key.indexOf("|") + 1) : key;
}

// Returns the trimmed value, or null unless it is a string of 1..max characters
// with no code unit below U+0020 (except "\n" when allowNewline).
function cleanField(value, max, allowNewline) {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  if (trimmed.length < 1 || trimmed.length > max) {
    return null;
  }
  for (let i = 0; i < trimmed.length; i++) {
    const code = trimmed.charCodeAt(i);
    if (code < 0x20 && !(allowNewline && code === 0x0a)) {
      return null;
    }
  }
  return trimmed;
}

function isActive(note) {
  return typeof note.cleared_at !== "string";
}

// Returns the note objects under id with the history trimmed: every active note
// plus the MAX_CLEARED_NOTES most recently cleared, in their original order.
function trimHistory(list) {
  const cleared = list
    .map((note, index) => ({ note, index }))
    .filter(({ note }) => !isActive(note));
  cleared.sort((a, b) => (a.note.cleared_at < b.note.cleared_at ? 1
    : a.note.cleared_at > b.note.cleared_at ? -1 : a.index - b.index));
  const keep = new Set(cleared.slice(0, MAX_CLEARED_NOTES).map(({ index }) => index));
  return list.filter((note, index) => isActive(note) || keep.has(index));
}

// Pure change functions: (notesFile) -> { error: Response } | { file, note }.
function addNoteChange(id, note) {
  return (file) => {
    const notes = file.notes;
    const exists = Object.prototype.hasOwnProperty.call(notes, id);
    if (!exists && Object.keys(notes).length >= MAX_NOTE_IDS) {
      return { error: json(413, { status: "notes_full" }) };
    }
    const list = exists && Array.isArray(notes[id]) ? notes[id].filter(isPlainObject) : [];
    if (list.filter(isActive).length >= MAX_ACTIVE_NOTES) {
      return { error: json(409, { status: "too_many_notes" }) };
    }
    list.push({ ...note });
    notes[id] = trimHistory(list);
    return { file, note };
  };
}

function clearNoteChange(id, noteId, author, reason, iso) {
  return (file) => {
    const notes = file.notes;
    const list = Object.prototype.hasOwnProperty.call(notes, id) && Array.isArray(notes[id])
      ? notes[id].filter(isPlainObject) : [];
    const target = list.find((note) => note.id === noteId);
    if (!target) {
      return { error: json(404, { status: "not_found" }) };
    }
    if (!isActive(target)) {
      return { error: json(409, { status: "already_cleared" }) };
    }
    target.cleared_at = iso;
    target.cleared_by = author;
    target.clear_reason = reason;
    notes[id] = trimHistory(list);
    return { file, note: normalizeNote(target) };
  };
}

// Returns a note with exactly the 7 note fields, or null if it is malformed.
function normalizeNote(item) {
  if (!isPlainObject(item) || !NOTE_FIELDS.every((f) => typeof item[f] === "string")) {
    return null;
  }
  const note = {};
  for (const f of NOTE_FIELDS) {
    note[f] = item[f];
  }
  for (const f of NOTE_CLEARED_FIELDS) {
    note[f] = typeof item[f] === "string" ? item[f] : null;
  }
  return note;
}

function sanitizeNotes(file) {
  const out = {};
  const notes = isPlainObject(file) && file.version === 1 && isPlainObject(file.notes)
    ? file.notes : {};
  for (const id of Object.keys(notes)) {
    if (!Array.isArray(notes[id])) {
      continue;
    }
    out[id] = notes[id].map(normalizeNote).filter((note) => note !== null);
  }
  return out;
}

// Returns { sha, file } (file is a valid notes object; sha null when missing)
// or { error } for an unexpected GitHub status.
async function readNotesFile(url, branch, headers) {
  const res = await fetch(url + "?ref=" + encodeURIComponent(branch), { method: "GET", headers });
  if (res.status === 404) {
    return { sha: null, file: { version: 1, notes: {} } };
  }
  if (res.status !== 200) {
    return { error: "github " + res.status };
  }
  const body = await res.json();
  let parsed = null;
  try {
    parsed = JSON.parse(Buffer.from(body.content, "base64").toString("utf8"));
  } catch {
    parsed = null;
  }
  const valid = isPlainObject(parsed) && parsed.version === 1 && isPlainObject(parsed.notes);
  return { sha: body.sha || null, file: valid ? parsed : { version: 1, notes: {} } };
}

// Reads, applies change and writes the notes file, retrying once on 409/422.
// Returns { note } or { response }.
async function saveNotes(change, message, { branch, headers, url }) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const current = await readNotesFile(url, branch, headers);
    if (current.error) {
      return { response: json(502, { status: "error", error: current.error }) };
    }
    const result = change(current.file);
    if (result.error) {
      return { response: result.error };
    }
    const content = Buffer.from(JSON.stringify(result.file), "utf8").toString("base64");
    const putBody = { message, content, branch };
    if (current.sha) {
      putBody.sha = current.sha;
    }
    const putRes = await fetch(url, {
      method: "PUT",
      headers: { ...headers, "content-type": "application/json" },
      body: JSON.stringify(putBody),
    });
    if (putRes.status === 200 || putRes.status === 201) {
      return { note: result.note };
    }
    if (attempt === 0 && (putRes.status === 409 || putRes.status === 422)) {
      continue;
    }
    return { response: json(502, { status: "error", error: "github " + putRes.status }) };
  }
}

async function handleNote(body, { token, repo, branch, headers }) {
  const key = typeof body.key === "string" && KEY_RE.test(body.key) ? body.key : null;
  const author = cleanField(body.author, 40, false);
  if (key === null || author === null) {
    return json(400, { status: "bad_request" });
  }
  const id = notesId(key);
  const iso = nowIso();
  let change;
  let message;
  if (body.action === "add_note") {
    const text = cleanField(body.text, 500, true);
    if (text === null) {
      return json(400, { status: "bad_request" });
    }
    change = addNoteChange(id, {
      id: crypto.randomUUID(),
      text,
      author,
      created_at: iso,
      cleared_at: null,
      cleared_by: null,
      clear_reason: null,
    });
    message = "note added " + iso;
  } else {
    const reason = cleanField(body.reason, 300, false);
    const noteId = body.note_id;
    if (reason === null || typeof noteId !== "string" || noteId.length < 1 || noteId.length > 64) {
      return json(400, { status: "bad_request" });
    }
    change = clearNoteChange(id, noteId, author, reason, iso);
    message = "note cleared " + iso;
  }

  const notesPath = process.env.NOTES_PATH || "summary-notes.json";
  let saved;
  try {
    saved = await saveNotes(change, message, {
      branch,
      headers,
      url: `https://api.github.com/repos/${repo}/contents/${notesPath}`,
    });
  } catch (err) {
    return json(502, { status: "error", error: "github " + redact(err, token) });
  }
  if (saved.response) {
    return saved.response;
  }

  let outcome;
  try {
    outcome = await queueResummarize(key, { token, branch, headers, url: resummarizeUrl(repo) });
  } catch {
    outcome = { kind: "error" };
  }
  let regenerate = "failed";
  let requestedAt = null;
  if (outcome.kind === "requested") {
    regenerate = "requested";
    requestedAt = outcome.requested_at;
  } else if (outcome.kind === "too_soon") {
    regenerate = "already_requested";
    requestedAt = outcome.requested_at;
  }
  return json(202, { status: "saved", note: saved.note, regenerate, requested_at: requestedAt });
}

async function readStatus() {
  const token = process.env.GITHUB_TOKEN;
  const repo = process.env.REBUILD_REPO;
  const branch = process.env.REBUILD_BRANCH || "rebuild-requests";
  const path = process.env.REBUILD_PATH || "rebuild-request.json";
  const resummarizePath = process.env.RESUMMARIZE_PATH || "resummarize-requests.json";
  const notesPath = process.env.NOTES_PATH || "summary-notes.json";
  if (!token || !repo) {
    return json(500, { status: "error", error: "not configured" });
  }

  const headers = githubHeaders(token);

  // Returns { file } (parsed content, or null if missing/unparseable) or { error }.
  async function readFile(filePath) {
    const url = `https://api.github.com/repos/${repo}/contents/${filePath}`;
    const res = await fetch(url + "?ref=" + encodeURIComponent(branch), { method: "GET", headers });
    if (res.status === 404) {
      return { file: null };
    }
    if (res.status !== 200) {
      return { error: "github " + res.status };
    }
    const body = await res.json();
    try {
      return { file: JSON.parse(Buffer.from(body.content, "base64").toString("utf8")) };
    } catch {
      return { file: null };
    }
  }

  try {
    const rebuild = await readFile(path);
    if (rebuild.error) {
      return json(502, { status: "error", error: rebuild.error });
    }
    const resummarize = await readFile(resummarizePath);
    if (resummarize.error) {
      return json(502, { status: "error", error: resummarize.error });
    }
    const notesFile = await readFile(notesPath);
    if (notesFile.error) {
      return json(502, { status: "error", error: notesFile.error });
    }

    const prior = rebuild.file;
    const rebuildRequestedAt = prior && typeof prior === "object" &&
      typeof prior.requested_at === "string" ? prior.requested_at : null;
    const priorRequests = resummarize.file && typeof resummarize.file === "object"
      ? resummarize.file.requests : null;
    const requests = {};
    if (priorRequests && typeof priorRequests === "object" && !Array.isArray(priorRequests)) {
      for (const k of Object.keys(priorRequests)) {
        if (typeof priorRequests[k] === "string") {
          requests[k] = priorRequests[k];
        }
      }
    }
    const notes = sanitizeNotes(notesFile.file);
    return json(200, { status: "ok", rebuild_requested_at: rebuildRequestedAt, requests, notes },
      { "cache-control": "no-store" });
  } catch (err) {
    return json(502, { status: "error", error: "github " + redact(err, token) });
  }
}
