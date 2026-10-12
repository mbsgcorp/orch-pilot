// Netlify Function: record a "rebuild requested" file on a branch of the site repo.
// Hermes polls that branch and rebuilds the script map; requests are limited to one
// per 10 minutes. Uses only Node built-ins (fetch, Response, Buffer, crypto).
//
// A POST whose body is {"key": "[NS|]LABEL:path"} instead records a per-script
// "regenerate summary" request in RESUMMARIZE_PATH on the same branch, as
// {"version": 1, "requests": {"<key>": "<iso>"}}, limited to one per key per 10
// minutes. Entries older than 7 days are dropped and at most 500 are kept.
// The body may also carry "author" (trimmed, at most 40 characters) and "guidance"
// (trimmed, at most 500 characters, newlines allowed). Before the resummarize file
// is written, these are saved in REGEN_DETAILS_PATH on the same branch, as
// {"version": 1, "requests": {"<key>": {"requested_at": "<iso>", "author": "...",
// "guidance": "..."}}}, with the same pruning. A 409/422 on either write is retried
// once after re-reading that file.
//
// A GET reads all three files (no writes) and returns {"status": "ok",
// "rebuild_requested_at": <iso or null>, "requests": {"<key>": "<iso>"},
// "regenerate_details": {"<key>": {"requested_at", "author", "guidance"}}} with
// cache-control: no-store, so the page can show which requests are still pending.

export const config = { path: "/api/rebuild" };

const MIN_INTERVAL_MS = 10 * 60 * 1000;
const MAX_REQUEST_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const MAX_REQUESTS = 500;
const KEY_RE = /^(?:[A-Za-z0-9_-]+\|)?[^|:\r\n]{1,100}:[^\r\n]{1,300}$/;
const BODY_KEYS = new Set(["key", "author", "guidance"]);
const MAX_AUTHOR = 40;
const MAX_GUIDANCE = 500;

function json(status, obj, extraHeaders = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", ...extraHeaders },
  });
}

function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function hasOwn(obj, name) {
  return Object.prototype.hasOwnProperty.call(obj, name);
}

// Returns the trimmed string for an optional body field ("" when missing), or null
// when it is not a string, too long, or has a control character (other than "\n"
// when allowNewline).
function optionalText(parsed, name, maxLength, allowNewline) {
  if (!hasOwn(parsed, name)) {
    return "";
  }
  if (typeof parsed[name] !== "string") {
    return null;
  }
  const value = parsed[name].trim();
  if (value.length > maxLength) {
    return null;
  }
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (code < 0x20 && !(allowNewline && code === 0x0a)) {
      return null;
    }
  }
  return value;
}

// Builds the next requests map: key -> value first, plus other entries whose
// timestampOf(value) is no older than MAX_REQUEST_AGE_MS; newest first, at most
// MAX_REQUESTS.
function pruneRequests(requests, key, value, now, timestampOf) {
  const kept = [[key, value, timestampOf(value)]];
  for (const k of Object.keys(requests)) {
    const v = requests[k];
    const when = timestampOf(v);
    if (k !== key && !Number.isNaN(when) && now - when <= MAX_REQUEST_AGE_MS) {
      kept.push([k, v, when]);
    }
  }
  kept.sort((a, b) => b[2] - a[2]);
  const next = {};
  for (const [k, v] of kept.slice(0, MAX_REQUESTS)) {
    next[k] = v;
  }
  return next;
}

function isoTimestamp(value) {
  return typeof value === "string" ? Date.parse(value) : NaN;
}

function detailsTimestamp(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value) &&
    typeof value.requested_at === "string" ? Date.parse(value.requested_at) : NaN;
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

  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "mbsg-script-map",
  };
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
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed) ||
        typeof parsed.key !== "string" || !KEY_RE.test(parsed.key) ||
        !Object.keys(parsed).every((k) => BODY_KEYS.has(k))) {
      return json(400, { status: "bad_request" });
    }
    const author = optionalText(parsed, "author", MAX_AUTHOR, false);
    const guidance = optionalText(parsed, "guidance", MAX_GUIDANCE, true);
    if (author === null || guidance === null) {
      return json(400, { status: "bad_request" });
    }
    const resummarizePath = process.env.RESUMMARIZE_PATH || "resummarize-requests.json";
    const detailsPath = process.env.REGEN_DETAILS_PATH || "regenerate-details.json";
    return requestResummarize(parsed.key, {
      token,
      branch,
      headers,
      author,
      guidance,
      url: `https://api.github.com/repos/${repo}/contents/${resummarizePath}`,
      detailsUrl: `https://api.github.com/repos/${repo}/contents/${detailsPath}`,
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
    const message = String((err && err.message) || err).split(token).join("***");
    return json(502, { status: "error", error: "github " + message });
  }
};

async function requestResummarize(key, { token, branch, headers, author, guidance, url, detailsUrl }) {
  // Reads a {"version": 1, "requests": {...}} file: { sha, requests } or { error }.
  // A missing file has no sha; unparseable content gives {} (keeping the sha).
  async function readRequests(fileUrl) {
    const getRes = await fetch(fileUrl + "?ref=" + encodeURIComponent(branch), { method: "GET", headers });
    if (getRes.status === 404) {
      return { sha: null, requests: {} };
    }
    if (getRes.status !== 200) {
      return { error: "github " + getRes.status };
    }
    const body = await getRes.json();
    let prior = null;
    try {
      prior = JSON.parse(Buffer.from(body.content, "base64").toString("utf8"));
    } catch {
      prior = null;
    }
    const priorRequests = prior && typeof prior === "object" ? prior.requests : null;
    if (priorRequests && typeof priorRequests === "object" && !Array.isArray(priorRequests)) {
      return { sha: body.sha, requests: priorRequests };
    }
    return { sha: body.sha, requests: {} };
  }

  // PUTs {"version": 1, "requests": next} and returns the GitHub status.
  async function writeRequests(fileUrl, message, sha, next) {
    const content = Buffer.from(
      JSON.stringify({ version: 1, requests: next }),
      "utf8",
    ).toString("base64");
    const putBody = { message, content, branch };
    if (sha) {
      putBody.sha = sha;
    }
    const putRes = await fetch(fileUrl, {
      method: "PUT",
      headers: { ...headers, "content-type": "application/json" },
      body: JSON.stringify(putBody),
    });
    return putRes.status;
  }

  // Returns a 429 response if key was requested within MIN_INTERVAL_MS, else null.
  function tooSoon(requests, now) {
    const requestedAt = hasOwn(requests, key) &&
      typeof requests[key] === "string" ? requests[key] : null;
    const t = requestedAt === null ? NaN : Date.parse(requestedAt);
    if (!Number.isNaN(t) && now - t < MIN_INTERVAL_MS) {
      return json(429, { status: "too_soon", requested_at: requestedAt });
    }
    return null;
  }

  const githubError = (status) => json(502, { status: "error", error: "github " + status });
  const written = (status) => status === 200 || status === 201;
  const retryable = (status) => status === 409 || status === 422;

  try {
    let resummarize = await readRequests(url);
    if (resummarize.error) {
      return json(502, { status: "error", error: resummarize.error });
    }
    let now = Date.now();
    let limited = tooSoon(resummarize.requests, now);
    if (limited) {
      return limited;
    }

    const iso = nowIso();
    const entry = { requested_at: iso, author, guidance };
    const detailsMessage = "regenerate details " + iso;

    let details = await readRequests(detailsUrl);
    if (details.error) {
      return json(502, { status: "error", error: details.error });
    }
    let status = await writeRequests(detailsUrl, detailsMessage, details.sha,
      pruneRequests(details.requests, key, entry, Date.now(), detailsTimestamp));
    if (retryable(status)) {
      details = await readRequests(detailsUrl);
      if (details.error) {
        return json(502, { status: "error", error: details.error });
      }
      status = await writeRequests(detailsUrl, detailsMessage, details.sha,
        pruneRequests(details.requests, key, entry, Date.now(), detailsTimestamp));
    }
    if (!written(status)) {
      return githubError(status);
    }

    const resummarizeMessage = "resummarize requested " + iso;
    status = await writeRequests(url, resummarizeMessage, resummarize.sha,
      pruneRequests(resummarize.requests, key, iso, now, isoTimestamp));
    if (retryable(status)) {
      resummarize = await readRequests(url);
      if (resummarize.error) {
        return json(502, { status: "error", error: resummarize.error });
      }
      now = Date.now();
      limited = tooSoon(resummarize.requests, now);
      if (limited) {
        return limited;
      }
      status = await writeRequests(url, resummarizeMessage, resummarize.sha,
        pruneRequests(resummarize.requests, key, iso, now, isoTimestamp));
    }
    if (!written(status)) {
      return githubError(status);
    }
    return json(202, { status: "requested", key, requested_at: iso });
  } catch (err) {
    const message = String((err && err.message) || err).split(token).join("***");
    return json(502, { status: "error", error: "github " + message });
  }
}

async function readStatus() {
  const token = process.env.GITHUB_TOKEN;
  const repo = process.env.REBUILD_REPO;
  const branch = process.env.REBUILD_BRANCH || "rebuild-requests";
  const path = process.env.REBUILD_PATH || "rebuild-request.json";
  const resummarizePath = process.env.RESUMMARIZE_PATH || "resummarize-requests.json";
  const detailsPath = process.env.REGEN_DETAILS_PATH || "regenerate-details.json";
  if (!token || !repo) {
    return json(500, { status: "error", error: "not configured" });
  }

  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "mbsg-script-map",
  };

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
    const details = await readFile(detailsPath);
    if (details.error) {
      return json(502, { status: "error", error: details.error });
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
    const priorDetails = details.file && typeof details.file === "object"
      ? details.file.requests : null;
    const regenerateDetails = {};
    if (priorDetails && typeof priorDetails === "object" && !Array.isArray(priorDetails)) {
      for (const k of Object.keys(priorDetails)) {
        const v = priorDetails[k];
        if (v !== null && typeof v === "object" && !Array.isArray(v) &&
            typeof v.requested_at === "string" && typeof v.author === "string" &&
            typeof v.guidance === "string") {
          regenerateDetails[k] = { requested_at: v.requested_at, author: v.author, guidance: v.guidance };
        }
      }
    }
    return json(200, {
      status: "ok",
      rebuild_requested_at: rebuildRequestedAt,
      requests,
      regenerate_details: regenerateDetails,
    }, { "cache-control": "no-store" });
  } catch (err) {
    const message = String((err && err.message) || err).split(token).join("***");
    return json(502, { status: "error", error: "github " + message });
  }
}
