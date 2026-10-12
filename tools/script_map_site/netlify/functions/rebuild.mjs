// Netlify Function: record a "rebuild requested" file on a branch of the site repo.
// Hermes polls that branch and rebuilds the script map; requests are limited to one
// per 10 minutes. Uses only Node built-ins (fetch, Response, Buffer, crypto).

export const config = { path: "/api/rebuild" };

const MIN_INTERVAL_MS = 10 * 60 * 1000;

function json(status, obj) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

export default async (req) => {
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
