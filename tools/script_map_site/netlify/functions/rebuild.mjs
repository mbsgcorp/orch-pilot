// Netlify Function: record a "rebuild requested" file on a branch of the site repo.
// Hermes polls that branch and rebuilds the script map; requests are limited to one
// per 10 minutes. Uses only Node built-ins (fetch, Response, Buffer, crypto).
//
// A POST whose body is {"key": "[NS|]LABEL:path"} instead records a per-script
// "regenerate summary" request in RESUMMARIZE_PATH on the same branch, as
// {"version": 1, "requests": {"<key>": "<iso>"}}, limited to one per key per 10
// minutes. Entries older than 7 days are dropped and at most 500 are kept.
//
// A GET reads both files (no writes) and returns {"status": "ok",
// "rebuild_requested_at": <iso or null>, "requests": {"<key>": "<iso>"}} with
// cache-control: no-store, so the page can show which requests are still pending.

export const config = { path: "/api/rebuild" };

const MIN_INTERVAL_MS = 10 * 60 * 1000;
const MAX_REQUEST_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const MAX_REQUESTS = 500;
const KEY_RE = /^(?:[A-Za-z0-9_-]+\|)?[^|:\r\n]{1,100}:[^\r\n]{1,300}$/;

function json(status, obj, extraHeaders = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", ...extraHeaders },
  });
}

function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
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
        typeof parsed.key !== "string" || !KEY_RE.test(parsed.key)) {
      return json(400, { status: "bad_request" });
    }
    const resummarizePath = process.env.RESUMMARIZE_PATH || "resummarize-requests.json";
    return requestResummarize(parsed.key, {
      token,
      branch,
      headers,
      url: `https://api.github.com/repos/${repo}/contents/${resummarizePath}`,
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

async function requestResummarize(key, { token, branch, headers, url }) {
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
      return json(502, { status: "error", error: "github " + getRes.status });
    }

    const now = Date.now();
    const requestedAt = Object.prototype.hasOwnProperty.call(requests, key) &&
      typeof requests[key] === "string" ? requests[key] : null;
    const t = requestedAt === null ? NaN : Date.parse(requestedAt);
    if (!Number.isNaN(t) && now - t < MIN_INTERVAL_MS) {
      return json(429, { status: "too_soon", requested_at: requestedAt });
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
      return json(202, { status: "requested", key, requested_at: iso });
    }
    return json(502, { status: "error", error: "github " + putRes.status });
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
    return json(200, { status: "ok", rebuild_requested_at: rebuildRequestedAt, requests },
      { "cache-control": "no-store" });
  } catch (err) {
    const message = String((err && err.message) || err).split(token).join("***");
    return json(502, { status: "error", error: "github " + message });
  }
}
