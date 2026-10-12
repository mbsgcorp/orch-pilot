import { test, afterEach } from "node:test";
import assert from "node:assert/strict";

const mod = await import(new URL("../tools/script_map_site/netlify/functions/rebuild.mjs", import.meta.url));
const handler = mod.default;

const TOKEN = "test-token-XYZ";
const ENV_KEYS = [
  "GITHUB_TOKEN", "REBUILD_REPO", "REBUILD_BRANCH", "REBUILD_PATH", "RESUMMARIZE_PATH", "REGEN_DETAILS_PATH",
];
const originalFetch = globalThis.fetch;
const originalEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
const CONTENTS_URL = "https://api.github.com/repos/o/r/contents/rebuild-request.json";

function setEnv(values) {
  for (const k of ENV_KEYS) {
    delete process.env[k];
  }
  Object.assign(process.env, values);
}

function configure() {
  setEnv({ GITHUB_TOKEN: TOKEN, REBUILD_REPO: "o/r" });
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  for (const k of ENV_KEYS) {
    if (originalEnv[k] === undefined) {
      delete process.env[k];
    } else {
      process.env[k] = originalEnv[k];
    }
  }
});

function jsonResponse(status, obj) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });
}

// Fake fetch: replies[i] answers the i-th call (a Response, or a function that throws).
function fakeFetch(replies) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    const reply = replies[calls.length - 1];
    if (reply === undefined) {
      throw new Error("unexpected fetch call " + calls.length);
    }
    return typeof reply === "function" ? reply() : reply;
  };
  return calls;
}

function post() {
  return new Request("http://localhost/api/rebuild", { method: "POST" });
}

function priorFile(minutesAgo) {
  const iso = new Date(Date.now() - minutesAgo * 60000).toISOString().replace(/\.\d{3}Z$/, "Z");
  return {
    iso,
    body: { sha: "abc", content: Buffer.from(JSON.stringify({ requested_at: iso })).toString("base64") },
  };
}

async function read(res) {
  const text = await res.text();
  assert.equal(res.headers.get("content-type"), "application/json");
  assert.ok(!text.includes(TOKEN), "token leaked in response body");
  return JSON.parse(text);
}

function putBody(call) {
  assert.equal(call.init.method, "PUT");
  assert.equal(call.url, CONTENTS_URL);
  return JSON.parse(call.init.body);
}

function assertRequested(body) {
  assert.equal(body.status, "requested");
  assert.match(body.requested_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
}

test("config.path is /api/rebuild", () => {
  assert.equal(mod.config.path, "/api/rebuild");
});

test("DELETE returns 405", async () => {
  configure();
  const calls = fakeFetch([]);
  const res = await handler(new Request("http://localhost/api/rebuild", { method: "DELETE" }));
  assert.equal(res.status, 405);
  assert.deepEqual(await read(res), { status: "method_not_allowed" });
  assert.equal(calls.length, 0);
});

test("missing env returns 500", async () => {
  for (const env of [{}, { GITHUB_TOKEN: TOKEN }, { REBUILD_REPO: "o/r" }]) {
    setEnv(env);
    const calls = fakeFetch([]);
    const res = await handler(post());
    assert.equal(res.status, 500);
    assert.deepEqual(await read(res), { status: "error", error: "not configured" });
    assert.equal(calls.length, 0);
  }
});

test("no prior file: PUT without sha, 202", async () => {
  configure();
  const calls = fakeFetch([jsonResponse(404, { message: "Not Found" }), jsonResponse(201, {})]);
  const res = await handler(post());
  assert.equal(res.status, 202);
  const body = await read(res);
  assertRequested(body);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[0].url, CONTENTS_URL + "?ref=rebuild-requests");
  const put = putBody(calls[1]);
  assert.ok(!("sha" in put));
  assert.equal(put.branch, "rebuild-requests");
  assert.equal(put.message, "rebuild requested " + body.requested_at);
  const content = JSON.parse(Buffer.from(put.content, "base64").toString("utf8"));
  assert.deepEqual(Object.keys(content).sort(), ["id", "requested_at"]);
  assert.equal(content.requested_at, body.requested_at);
  assert.match(content.id, /^[0-9a-f-]{36}$/);
});

test("request headers include auth, API version and User-Agent", async () => {
  configure();
  const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(201, {})]);
  await handler(post());
  for (const call of calls) {
    const h = new Headers(call.init.headers);
    assert.equal(h.get("authorization"), "Bearer " + TOKEN);
    assert.equal(h.get("accept"), "application/vnd.github+json");
    assert.equal(h.get("x-github-api-version"), "2022-11-28");
    assert.equal(h.get("user-agent"), "mbsg-script-map");
  }
  assert.equal(new Headers(calls[1].init.headers).get("content-type"), "application/json");
});

test("custom branch and path are used", async () => {
  setEnv({ GITHUB_TOKEN: TOKEN, REBUILD_REPO: "o/r", REBUILD_BRANCH: "b x", REBUILD_PATH: "dir/f.json" });
  const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(200, {})]);
  const res = await handler(post());
  assert.equal(res.status, 202);
  assert.equal(calls[0].url, "https://api.github.com/repos/o/r/contents/dir/f.json?ref=b%20x");
  assert.equal(calls[1].url, "https://api.github.com/repos/o/r/contents/dir/f.json");
  assert.equal(JSON.parse(calls[1].init.body).branch, "b x");
});

test("prior request 3 minutes old: 429, no PUT", async () => {
  configure();
  const prior = priorFile(3);
  const calls = fakeFetch([jsonResponse(200, prior.body)]);
  const res = await handler(post());
  assert.equal(res.status, 429);
  assert.deepEqual(await read(res), { status: "too_soon", requested_at: prior.iso });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].init.method, "GET");
});

test("prior request in the future: 429", async () => {
  configure();
  const prior = priorFile(-5);
  const calls = fakeFetch([jsonResponse(200, prior.body)]);
  const res = await handler(post());
  assert.equal(res.status, 429);
  assert.equal(calls.length, 1);
});

test("prior request 11 minutes old: PUT with sha, 202", async () => {
  configure();
  const prior = priorFile(11);
  const calls = fakeFetch([jsonResponse(200, prior.body), jsonResponse(200, {})]);
  const res = await handler(post());
  assert.equal(res.status, 202);
  assertRequested(await read(res));
  assert.equal(calls.length, 2);
  assert.equal(putBody(calls[1]).sha, "abc");
});

test("unparseable prior content: PUT with sha, 202", async () => {
  configure();
  const body = { sha: "abc", content: Buffer.from("not json{").toString("base64") };
  const calls = fakeFetch([jsonResponse(200, body), jsonResponse(201, {})]);
  const res = await handler(post());
  assert.equal(res.status, 202);
  assertRequested(await read(res));
  assert.equal(calls.length, 2);
  assert.equal(putBody(calls[1]).sha, "abc");
});

test("GitHub 500 on GET: 502, no PUT", async () => {
  configure();
  const calls = fakeFetch([jsonResponse(500, {})]);
  const res = await handler(post());
  assert.equal(res.status, 502);
  assert.deepEqual(await read(res), { status: "error", error: "github 500" });
  assert.equal(calls.length, 1);
});

test("PUT returning 422: 502", async () => {
  configure();
  const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(422, { message: "sha mismatch" })]);
  const res = await handler(post());
  assert.equal(res.status, 502);
  assert.deepEqual(await read(res), { status: "error", error: "github 422" });
  assert.equal(calls.length, 2);
});

test("fetch throwing: 502 with message, token not leaked", async () => {
  configure();
  fakeFetch([() => { throw new Error("connect failed " + TOKEN); }]);
  const res = await handler(post());
  assert.equal(res.status, 502);
  const body = await read(res);
  assert.equal(body.status, "error");
  assert.match(body.error, /^github connect failed/);
});

// Per-script "regenerate summary" requests: POST {"key": "[NS|]LABEL:path"}.

const RESUMMARIZE_URL = "https://api.github.com/repos/o/r/contents/resummarize-requests.json";
const DETAILS_URL = "https://api.github.com/repos/o/r/contents/regenerate-details.json";

function postBody(text) {
  return new Request("http://localhost/api/rebuild", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: text,
  });
}

function postKey(key) {
  return postBody(JSON.stringify({ key }));
}

function isoMinutesAgo(minutes) {
  return new Date(Date.now() - minutes * 60000).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function resummarizeFile(requests) {
  const content = Buffer.from(JSON.stringify({ version: 1, requests })).toString("base64");
  return { sha: "def", content };
}

function resummarizePut(call) {
  assert.equal(call.init.method, "PUT");
  assert.equal(call.url, RESUMMARIZE_URL);
  const put = JSON.parse(call.init.body);
  put.text = Buffer.from(put.content, "base64").toString("utf8");
  put.file = JSON.parse(put.text);
  return put;
}

function detailsFile(requests) {
  const content = Buffer.from(JSON.stringify({ version: 1, requests })).toString("base64");
  return { sha: "ghi", content };
}

function detailsPut(call, url = DETAILS_URL) {
  assert.equal(call.init.method, "PUT");
  assert.equal(call.url, url);
  const put = JSON.parse(call.init.body);
  put.file = JSON.parse(Buffer.from(put.content, "base64").toString("utf8"));
  return put;
}

function postJson(obj) {
  return postBody(JSON.stringify(obj));
}

// Replies for an accepted key request with both files missing and both PUTs succeeding.
function acceptedReplies() {
  return [jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(201, {}), jsonResponse(201, {})];
}

test("key request: invalid bodies give 400 with no GitHub calls", async () => {
  const bodies = [
    "not json{",
    "null",
    "[]",
    '"A:x.js"',
    "42",
    "{}",
    JSON.stringify({ key: 5 }),
    JSON.stringify({ key: null }),
    JSON.stringify({ key: "" }),
    JSON.stringify({ key: "no-colon" }),
    JSON.stringify({ key: "A:" }),
    JSON.stringify({ key: ":x.js" }),
    JSON.stringify({ key: "bad ns|A:x.js" }),
    JSON.stringify({ key: "a|b|A:x.js" }),
    JSON.stringify({ key: "A:x.js\nmore" }),
    JSON.stringify({ key: "A:x.js\r" }),
    JSON.stringify({ key: "A".repeat(101) + ":x.js" }),
    JSON.stringify({ key: "A:" + "x".repeat(301) }),
    " ",
  ];
  for (const text of bodies) {
    configure();
    const calls = fakeFetch([]);
    const res = await handler(postBody(text));
    assert.equal(res.status, 400, text);
    assert.deepEqual(await read(res), { status: "bad_request" });
    assert.equal(calls.length, 0, text);
  }
});

test("key request: no file, PUT without sha, 202 with key", async () => {
  configure();
  const key = "sandbox|RTMH:Sub Dir/ue.js";
  const calls = fakeFetch([
    jsonResponse(404, { message: "Not Found" }),
    jsonResponse(404, { message: "Not Found" }),
    jsonResponse(201, {}),
    jsonResponse(201, {}),
  ]);
  const res = await handler(postKey(key));
  assert.equal(res.status, 202);
  const body = await read(res);
  assert.deepEqual(Object.keys(body).sort(), ["key", "requested_at", "status"]);
  assertRequested(body);
  assert.equal(body.key, key);
  assert.equal(calls.length, 4);
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[0].url, RESUMMARIZE_URL + "?ref=rebuild-requests");
  assert.equal(calls[1].init.method, "GET");
  assert.equal(calls[1].url, DETAILS_URL + "?ref=rebuild-requests");
  detailsPut(calls[2]);
  const put = resummarizePut(calls[3]);
  assert.ok(!("sha" in put));
  assert.equal(put.branch, "rebuild-requests");
  assert.equal(put.message, "resummarize requested " + body.requested_at);
  assert.deepEqual(put.file, { version: 1, requests: { [key]: body.requested_at } });
  for (const call of calls) {
    const h = new Headers(call.init.headers);
    assert.equal(h.get("authorization"), "Bearer " + TOKEN);
    assert.equal(h.get("accept"), "application/vnd.github+json");
    assert.equal(h.get("x-github-api-version"), "2022-11-28");
    assert.equal(h.get("user-agent"), "mbsg-script-map");
  }
});

test("key request: custom RESUMMARIZE_PATH and branch are used", async () => {
  setEnv({ GITHUB_TOKEN: TOKEN, REBUILD_REPO: "o/r", REBUILD_BRANCH: "b x" });
  process.env.RESUMMARIZE_PATH = "dir/r.json";
  try {
    const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(200, {}),
      jsonResponse(200, {})]);
    const res = await handler(postKey("A:x.js"));
    assert.equal(res.status, 202);
    assert.equal(calls[0].url, "https://api.github.com/repos/o/r/contents/dir/r.json?ref=b%20x");
    assert.equal(calls[1].url, DETAILS_URL + "?ref=b%20x");
    assert.equal(calls[2].url, DETAILS_URL);
    assert.equal(JSON.parse(calls[2].init.body).branch, "b x");
    assert.equal(calls[3].url, "https://api.github.com/repos/o/r/contents/dir/r.json");
    assert.equal(JSON.parse(calls[3].init.body).branch, "b x");
  } finally {
    delete process.env.RESUMMARIZE_PATH;
  }
});

test("key request: same key 3 minutes old gives 429 and no PUT", async () => {
  configure();
  const iso = isoMinutesAgo(3);
  const calls = fakeFetch([jsonResponse(200, resummarizeFile({ "A:x.js": iso }))]);
  const res = await handler(postKey("A:x.js"));
  assert.equal(res.status, 429);
  assert.deepEqual(await read(res), { status: "too_soon", requested_at: iso });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].init.method, "GET");
});

test("key request: same key in the future gives 429", async () => {
  configure();
  const calls = fakeFetch([jsonResponse(200, resummarizeFile({ "A:x.js": isoMinutesAgo(-5) }))]);
  const res = await handler(postKey("A:x.js"));
  assert.equal(res.status, 429);
  assert.equal(calls.length, 1);
});

test("key request: same key 11 minutes old, PUT with sha, 202", async () => {
  configure();
  const calls = fakeFetch([
    jsonResponse(200, resummarizeFile({ "A:x.js": isoMinutesAgo(11) })),
    jsonResponse(404, {}),
    jsonResponse(201, {}),
    jsonResponse(200, {}),
  ]);
  const res = await handler(postKey("A:x.js"));
  assert.equal(res.status, 202);
  const body = await read(res);
  const put = resummarizePut(calls[3]);
  assert.equal(put.sha, "def");
  assert.deepEqual(put.file.requests, { "A:x.js": body.requested_at });
});

test("key request: a different key while another is recent gives 202", async () => {
  configure();
  const recent = isoMinutesAgo(2);
  const calls = fakeFetch([
    jsonResponse(200, resummarizeFile({ "A:x.js": recent })),
    jsonResponse(404, {}),
    jsonResponse(201, {}),
    jsonResponse(200, {}),
  ]);
  const res = await handler(postKey("sandbox|A:x.js"));
  assert.equal(res.status, 202);
  const body = await read(res);
  assert.equal(body.key, "sandbox|A:x.js");
  const put = resummarizePut(calls[3]);
  assert.equal(put.sha, "def");
  assert.deepEqual(put.file.requests, { "A:x.js": recent, "sandbox|A:x.js": body.requested_at });
});

test("key request: entries older than 7 days are dropped", async () => {
  configure();
  const fresh = isoMinutesAgo(60 * 24 * 6);
  const calls = fakeFetch([
    jsonResponse(200, resummarizeFile({
      "A:old.js": isoMinutesAgo(60 * 24 * 7 + 1),
      "A:fresh.js": fresh,
      "A:bad.js": "not a date",
      "A:num.js": 5,
    })),
    jsonResponse(404, {}),
    jsonResponse(201, {}),
    jsonResponse(201, {}),
  ]);
  const res = await handler(postKey("A:x.js"));
  assert.equal(res.status, 202);
  const body = await read(res);
  assert.deepEqual(resummarizePut(calls[3]).file.requests,
    { "A:fresh.js": fresh, "A:x.js": body.requested_at });
});

test("key request: at most 500 newest entries are kept", async () => {
  configure();
  const requests = {};
  for (let i = 0; i < 600; i++) {
    requests["A:f" + i + ".js"] = isoMinutesAgo(11 + i);
  }
  const calls = fakeFetch([jsonResponse(200, resummarizeFile(requests)), jsonResponse(404, {}),
    jsonResponse(201, {}), jsonResponse(201, {})]);
  const res = await handler(postKey("A:x.js"));
  assert.equal(res.status, 202);
  const body = await read(res);
  const kept = resummarizePut(calls[3]).file.requests;
  assert.equal(Object.keys(kept).length, 500);
  assert.equal(kept["A:x.js"], body.requested_at);
  for (let i = 0; i < 499; i++) {
    assert.equal(kept["A:f" + i + ".js"], requests["A:f" + i + ".js"]);
  }
  assert.ok(!("A:f499.js" in kept));
  assert.ok(!("A:f599.js" in kept));
});

test("key request: unparseable file, PUT with sha, 202", async () => {
  configure();
  for (const text of ["not json{", "[1]", '{"version":1,"requests":[]}', "null"]) {
    const file = { sha: "def", content: Buffer.from(text).toString("base64") };
    const calls = fakeFetch([jsonResponse(200, file), jsonResponse(404, {}), jsonResponse(201, {}),
      jsonResponse(201, {})]);
    const res = await handler(postKey("A:x.js"));
    assert.equal(res.status, 202, text);
    const body = await read(res);
    const put = resummarizePut(calls[3]);
    assert.equal(put.sha, "def");
    assert.deepEqual(put.file, { version: 1, requests: { "A:x.js": body.requested_at } });
  }
});

test("key request: GitHub 500 on GET gives 502, no PUT", async () => {
  configure();
  const calls = fakeFetch([jsonResponse(500, {})]);
  const res = await handler(postKey("A:x.js"));
  assert.equal(res.status, 502);
  assert.deepEqual(await read(res), { status: "error", error: "github 500" });
  assert.equal(calls.length, 1);
});

test("key request: resummarize PUT returning 409 twice gives 502", async () => {
  configure();
  const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(201, {}),
    jsonResponse(409, {}), jsonResponse(404, {}), jsonResponse(409, {})]);
  const res = await handler(postKey("A:x.js"));
  assert.equal(res.status, 502);
  assert.deepEqual(await read(res), { status: "error", error: "github 409" });
  assert.equal(calls.length, 6);
  assert.equal(calls[4].init.method, "GET");
  assert.equal(calls[4].url, RESUMMARIZE_URL + "?ref=rebuild-requests");
  resummarizePut(calls[5]);
});

test("key request: fetch throwing gives 502, token not leaked", async () => {
  configure();
  for (const replies of [
    [() => { throw new Error("connect failed " + TOKEN); }],
    [jsonResponse(404, {}), jsonResponse(404, {}), () => { throw new Error("put failed " + TOKEN); }],
    [jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(201, {}),
      () => { throw new Error("put failed " + TOKEN); }],
  ]) {
    fakeFetch(replies);
    const res = await handler(postKey("A:x.js"));
    assert.equal(res.status, 502);
    const body = await read(res);
    assert.equal(body.status, "error");
    assert.match(body.error, /^github (connect|put) failed \*\*\*$/);
  }
});

test("empty body still takes the full-rebuild path", async () => {
  configure();
  const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(201, {})]);
  const res = await handler(postBody(""));
  assert.equal(res.status, 202);
  const body = await read(res);
  assertRequested(body);
  assert.ok(!("key" in body));
  assert.equal(calls[0].url, CONTENTS_URL + "?ref=rebuild-requests");
  assert.equal(putBody(calls[1]).message, "rebuild requested " + body.requested_at);
});

// Status read: GET returns both request files, with no writes.

function get() {
  return new Request("http://localhost/api/rebuild", { method: "GET" });
}

function fileBody(obj) {
  return { sha: "s", content: Buffer.from(JSON.stringify(obj)).toString("base64") };
}

function assertAllGets(calls) {
  for (const call of calls) {
    assert.equal(call.init.method, "GET");
    assert.ok(!("body" in call.init));
  }
}

test("GET: both files present gives 200 with no-store", async () => {
  configure();
  const calls = fakeFetch([
    jsonResponse(200, fileBody({ requested_at: "2026-10-12T09:58:00Z", id: "x" })),
    jsonResponse(200, fileBody({ version: 1, requests: {
      "A:x.js": "2026-10-12T10:00:00Z", "sandbox|A:y.js": "2026-10-12T10:01:00Z" } })),
    jsonResponse(200, fileBody({ version: 1, requests: {
      "A:x.js": { requested_at: "2026-10-12T10:00:00Z", author: "Kim", guidance: "Short.\nPlease." } } })),
  ]);
  const res = await handler(get());
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.deepEqual(await read(res), {
    status: "ok",
    rebuild_requested_at: "2026-10-12T09:58:00Z",
    requests: { "A:x.js": "2026-10-12T10:00:00Z", "sandbox|A:y.js": "2026-10-12T10:01:00Z" },
    regenerate_details: {
      "A:x.js": { requested_at: "2026-10-12T10:00:00Z", author: "Kim", guidance: "Short.\nPlease." },
    },
  });
  assert.equal(calls.length, 3);
  assert.equal(calls[0].url, CONTENTS_URL + "?ref=rebuild-requests");
  assert.equal(calls[1].url, RESUMMARIZE_URL + "?ref=rebuild-requests");
  assert.equal(calls[2].url, DETAILS_URL + "?ref=rebuild-requests");
  assertAllGets(calls);
  for (const call of calls) {
    const h = new Headers(call.init.headers);
    assert.equal(h.get("authorization"), "Bearer " + TOKEN);
    assert.equal(h.get("accept"), "application/vnd.github+json");
    assert.equal(h.get("x-github-api-version"), "2022-11-28");
    assert.equal(h.get("user-agent"), "mbsg-script-map");
  }
});

test("GET: custom branch and paths are used", async () => {
  setEnv({ GITHUB_TOKEN: TOKEN, REBUILD_REPO: "o/r", REBUILD_BRANCH: "b x", REBUILD_PATH: "dir/f.json" });
  process.env.RESUMMARIZE_PATH = "dir/r.json";
  process.env.REGEN_DETAILS_PATH = "dir/d.json";
  try {
    const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(404, {})]);
    const res = await handler(get());
    assert.equal(res.status, 200);
    assert.equal(calls[0].url, "https://api.github.com/repos/o/r/contents/dir/f.json?ref=b%20x");
    assert.equal(calls[1].url, "https://api.github.com/repos/o/r/contents/dir/r.json?ref=b%20x");
    assert.equal(calls[2].url, "https://api.github.com/repos/o/r/contents/dir/d.json?ref=b%20x");
    assertAllGets(calls);
  } finally {
    delete process.env.RESUMMARIZE_PATH;
    delete process.env.REGEN_DETAILS_PATH;
  }
});

test("GET: all files missing gives null and {}", async () => {
  configure();
  const calls = fakeFetch([jsonResponse(404, { message: "Not Found" }), jsonResponse(404, {}),
    jsonResponse(404, {})]);
  const res = await handler(get());
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.deepEqual(await read(res),
    { status: "ok", rebuild_requested_at: null, requests: {}, regenerate_details: {} });
  assert.equal(calls.length, 3);
  assertAllGets(calls);
});

test("GET: malformed files give null and {}", async () => {
  const cases = [
    ["not json{", "not json{"],
    ["null", "null"],
    ["[1]", '{"version":1,"requests":[]}'],
    ['{"requested_at":5}', '{"version":1,"requests":"x"}'],
    ['"2026-10-12T09:58:00Z"', '{"version":1}'],
  ];
  for (const [a, b] of cases) {
    configure();
    const calls = fakeFetch([
      jsonResponse(200, { sha: "s", content: Buffer.from(a).toString("base64") }),
      jsonResponse(200, { sha: "s", content: Buffer.from(b).toString("base64") }),
      jsonResponse(200, { sha: "s", content: Buffer.from(b).toString("base64") }),
    ]);
    const res = await handler(get());
    assert.equal(res.status, 200, a);
    assert.deepEqual(await read(res),
      { status: "ok", rebuild_requested_at: null, requests: {}, regenerate_details: {} }, a);
    assertAllGets(calls);
  }
  configure();
  fakeFetch([jsonResponse(200, { sha: "s" }), jsonResponse(200, { content: 5 }), jsonResponse(200, { sha: "s" })]);
  const res = await handler(get());
  assert.equal(res.status, 200);
  assert.deepEqual(await read(res),
    { status: "ok", rebuild_requested_at: null, requests: {}, regenerate_details: {} });
});

test("GET: non-string request values are dropped", async () => {
  configure();
  fakeFetch([
    jsonResponse(404, {}),
    jsonResponse(200, fileBody({ version: 1, requests: {
      "A:ok.js": "2026-10-12T10:00:00Z", "A:num.js": 5, "A:null.js": null,
      "A:obj.js": { t: "x" }, "A:bad.js": "not a date" } })),
    jsonResponse(404, {}),
  ]);
  const res = await handler(get());
  assert.equal(res.status, 200);
  assert.deepEqual((await read(res)).requests,
    { "A:ok.js": "2026-10-12T10:00:00Z", "A:bad.js": "not a date" });
});

test("GET: GitHub 500 gives 502, no PUT", async () => {
  for (const replies of [
    [jsonResponse(500, {})],
    [jsonResponse(404, {}), jsonResponse(500, {})],
  ]) {
    configure();
    const calls = fakeFetch(replies);
    const res = await handler(get());
    assert.equal(res.status, 502);
    assert.deepEqual(await read(res), { status: "error", error: "github 500" });
    assert.equal(calls.length, replies.length);
    assertAllGets(calls);
  }
});

test("GET: fetch throwing gives 502, token redacted", async () => {
  for (const replies of [
    [() => { throw new Error("connect failed " + TOKEN); }],
    [jsonResponse(404, {}), () => { throw new Error("read failed " + TOKEN); }],
  ]) {
    configure();
    const calls = fakeFetch(replies);
    const res = await handler(get());
    assert.equal(res.status, 502);
    const body = await read(res);
    assert.equal(body.status, "error");
    assert.match(body.error, /^github (connect|read) failed \*\*\*$/);
    assertAllGets(calls);
  }
});

test("GET: missing env gives 500 with no GitHub calls", async () => {
  for (const env of [{}, { GITHUB_TOKEN: TOKEN }, { REBUILD_REPO: "o/r" }]) {
    setEnv(env);
    const calls = fakeFetch([]);
    const res = await handler(get());
    assert.equal(res.status, 500);
    assert.deepEqual(await read(res), { status: "error", error: "not configured" });
    assert.equal(calls.length, 0);
  }
});

test("other methods still return 405", async () => {
  for (const method of ["PUT", "PATCH", "DELETE"]) {
    configure();
    const calls = fakeFetch([]);
    const res = await handler(new Request("http://localhost/api/rebuild", { method }));
    assert.equal(res.status, 405, method);
    assert.deepEqual(await read(res), { status: "method_not_allowed" });
    assert.equal(calls.length, 0);
  }
});

// Regenerate details: POST {"key", "author"?, "guidance"?} also writes REGEN_DETAILS_PATH.

test("details: key only writes both files in order with empty author and guidance", async () => {
  configure();
  const calls = fakeFetch(acceptedReplies());
  const res = await handler(postKey("A:x.js"));
  assert.equal(res.status, 202);
  const body = await read(res);
  assertRequested(body);
  assert.deepEqual(Object.keys(body).sort(), ["key", "requested_at", "status"]);
  assert.equal(calls.length, 4);
  assert.deepEqual(calls.map((c) => [c.init.method, c.url]), [
    ["GET", RESUMMARIZE_URL + "?ref=rebuild-requests"],
    ["GET", DETAILS_URL + "?ref=rebuild-requests"],
    ["PUT", DETAILS_URL],
    ["PUT", RESUMMARIZE_URL],
  ]);
  const details = detailsPut(calls[2]);
  assert.ok(!("sha" in details));
  assert.equal(details.branch, "rebuild-requests");
  assert.equal(details.message, "regenerate details " + body.requested_at);
  assert.deepEqual(details.file, { version: 1, requests: {
    "A:x.js": { requested_at: body.requested_at, author: "", guidance: "" } } });
  const resummarize = resummarizePut(calls[3]);
  assert.equal(resummarize.message, "resummarize requested " + body.requested_at);
  assert.equal(resummarize.text,
    JSON.stringify({ version: 1, requests: { "A:x.js": body.requested_at } }));
  assert.equal(new Headers(calls[2].init.headers).get("content-type"), "application/json");
  assert.equal(new Headers(calls[2].init.headers).get("authorization"), "Bearer " + TOKEN);
});

test("details: author and guidance are trimmed and stored, Unicode and newlines kept", async () => {
  configure();
  const key = "sandbox|RTMH:a.js";
  const guidance = "  Focus on the café\nflow — keep it short ✓  ";
  const calls = fakeFetch(acceptedReplies());
  const res = await handler(postJson({ key, author: "  Kim Lee ", guidance }));
  assert.equal(res.status, 202);
  const body = await read(res);
  assert.equal(body.key, key);
  assert.deepEqual(detailsPut(calls[2]).file.requests, {
    [key]: { requested_at: body.requested_at, author: "Kim Lee", guidance: guidance.trim() },
  });
  const resummarize = resummarizePut(calls[3]);
  assert.equal(resummarize.text, JSON.stringify({ version: 1, requests: { [key]: body.requested_at } }));
});

test("details: spaces-only author and guidance become empty, max lengths accepted", async () => {
  for (const [input, author, guidance] of [
    [{ author: "   ", guidance: " \n " }, "", ""],
    [{ author: "a".repeat(40), guidance: "g".repeat(500) }, "a".repeat(40), "g".repeat(500)],
    [{ author: " " + "a".repeat(40) + " ", guidance: "\n" + "g".repeat(500) + "\n" },
      "a".repeat(40), "g".repeat(500)],
    [{ author: "", guidance: "" }, "", ""],
  ]) {
    configure();
    const calls = fakeFetch(acceptedReplies());
    const res = await handler(postJson({ key: "A:x.js", ...input }));
    assert.equal(res.status, 202, JSON.stringify(input));
    const body = await read(res);
    assert.deepEqual(detailsPut(calls[2]).file.requests["A:x.js"],
      { requested_at: body.requested_at, author, guidance });
  }
});

test("details: invalid author, guidance or properties give 400 with no GitHub calls", async () => {
  const bodies = [
    { key: "A:x.js", author: "a".repeat(41) },
    { key: "A:x.js", guidance: "g".repeat(501) },
    { key: "A:x.js", author: "a\nb" },
    { key: "A:x.js", author: "a\tb" },
    { key: "A:x.js", author: "a\rb" },
    { key: "A:x.js", author: "a\u0000b" },
    { key: "A:x.js", guidance: "a\tb" },
    { key: "A:x.js", guidance: "a\rb" },
    { key: "A:x.js", guidance: "a\u001fb" },
    { key: "A:x.js", author: 5 },
    { key: "A:x.js", author: null },
    { key: "A:x.js", author: ["Kim"] },
    { key: "A:x.js", author: { name: "Kim" } },
    { key: "A:x.js", author: true },
    { key: "A:x.js", guidance: null },
    { key: "A:x.js", guidance: 5 },
    { key: "A:x.js", guidance: [] },
    { key: "A:x.js", extra: "x" },
    { key: "A:x.js", author: "Kim", guidance: "g", note: "" },
    { key: "A:x.js", Author: "Kim" },
    { author: "Kim" },
    { key: "bad", author: "Kim" },
  ];
  const texts = bodies.map((b) => JSON.stringify(b));
  texts.push('{"key":"A:x.js","__proto__":{"author":"x"}}', "[]", "42", "null", '["A:x.js"]');
  for (const text of texts) {
    configure();
    const calls = fakeFetch([]);
    const res = await handler(postBody(text));
    assert.equal(res.status, 400, text);
    assert.deepEqual(await read(res), { status: "bad_request" });
    assert.equal(calls.length, 0, text);
  }
});

test("details: rate-limited request does no details read or write", async () => {
  configure();
  const iso = isoMinutesAgo(3);
  const calls = fakeFetch([jsonResponse(200, resummarizeFile({ "A:x.js": iso }))]);
  const res = await handler(postJson({ key: "A:x.js", author: "Kim", guidance: "again" }));
  assert.equal(res.status, 429);
  assert.deepEqual(await read(res), { status: "too_soon", requested_at: iso });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, RESUMMARIZE_URL + "?ref=rebuild-requests");
});

test("details: GitHub 500 on details GET gives 502 with no writes", async () => {
  configure();
  const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(500, {})]);
  const res = await handler(postJson({ key: "A:x.js", author: "Kim" }));
  assert.equal(res.status, 502);
  assert.deepEqual(await read(res), { status: "error", error: "github 500" });
  assert.equal(calls.length, 2);
  assertAllGets(calls);
});

test("details: details PUT failing gives 502 with no resummarize write", async () => {
  for (const status of [500, 403, 404]) {
    configure();
    const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(status, {})]);
    const res = await handler(postJson({ key: "A:x.js", author: "Kim" }));
    assert.equal(res.status, 502);
    assert.deepEqual(await read(res), { status: "error", error: "github " + status });
    assert.equal(calls.length, 3);
    detailsPut(calls[2]);
  }
});

test("details: details PUT 409/422 retried once then succeeding", async () => {
  for (const status of [409, 422]) {
    configure();
    const other = isoMinutesAgo(1);
    const calls = fakeFetch([
      jsonResponse(404, {}),
      jsonResponse(404, {}),
      jsonResponse(status, {}),
      jsonResponse(200, detailsFile({ "B:y.js": { requested_at: other, author: "Ann", guidance: "" } })),
      jsonResponse(200, {}),
      jsonResponse(201, {}),
    ]);
    const res = await handler(postJson({ key: "A:x.js", author: "Kim", guidance: "why" }));
    assert.equal(res.status, 202, String(status));
    const body = await read(res);
    assert.equal(calls.length, 6);
    assert.equal(calls[3].init.method, "GET");
    assert.equal(calls[3].url, DETAILS_URL + "?ref=rebuild-requests");
    const first = detailsPut(calls[2]);
    const second = detailsPut(calls[4]);
    assert.ok(!("sha" in first));
    assert.equal(second.sha, "ghi");
    assert.equal(second.message, first.message);
    assert.deepEqual(second.file.requests, {
      "A:x.js": { requested_at: body.requested_at, author: "Kim", guidance: "why" },
      "B:y.js": { requested_at: other, author: "Ann", guidance: "" },
    });
    assert.equal(resummarizePut(calls[5]).text,
      JSON.stringify({ version: 1, requests: { "A:x.js": body.requested_at } }));
  }
});

test("details: details PUT failing twice gives 502 with no resummarize write", async () => {
  for (const [first, second] of [[409, 409], [422, 500], [409, 422]]) {
    configure();
    const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(first, {}),
      jsonResponse(404, {}), jsonResponse(second, {})]);
    const res = await handler(postJson({ key: "A:x.js" }));
    assert.equal(res.status, 502);
    assert.deepEqual(await read(res), { status: "error", error: "github " + second });
    assert.equal(calls.length, 5);
    detailsPut(calls[4]);
  }
  configure();
  const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(409, {}),
    jsonResponse(500, {})]);
  const res = await handler(postJson({ key: "A:x.js" }));
  assert.equal(res.status, 502);
  assert.deepEqual(await read(res), { status: "error", error: "github 500" });
  assert.equal(calls.length, 4);
});

test("details: resummarize PUT 409/422 retried once then succeeding", async () => {
  for (const status of [409, 422]) {
    configure();
    const other = isoMinutesAgo(1);
    const calls = fakeFetch([
      jsonResponse(404, {}),
      jsonResponse(404, {}),
      jsonResponse(201, {}),
      jsonResponse(status, {}),
      jsonResponse(200, resummarizeFile({ "B:y.js": other })),
      jsonResponse(200, {}),
    ]);
    const res = await handler(postJson({ key: "A:x.js", author: "Kim" }));
    assert.equal(res.status, 202, String(status));
    const body = await read(res);
    assert.equal(calls.length, 6);
    assert.equal(calls[4].init.method, "GET");
    assert.equal(calls[4].url, RESUMMARIZE_URL + "?ref=rebuild-requests");
    const first = resummarizePut(calls[3]);
    const second = resummarizePut(calls[5]);
    assert.ok(!("sha" in first));
    assert.equal(second.sha, "def");
    assert.equal(second.message, "resummarize requested " + body.requested_at);
    assert.equal(second.text,
      JSON.stringify({ version: 1, requests: { "A:x.js": body.requested_at, "B:y.js": other } }));
    assert.equal(detailsPut(calls[2]).file.requests["A:x.js"].requested_at, body.requested_at);
  }
});

test("details: resummarize retry re-checks the rate limit", async () => {
  configure();
  const iso = isoMinutesAgo(0);
  const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(201, {}),
    jsonResponse(409, {}), jsonResponse(200, resummarizeFile({ "A:x.js": iso }))]);
  const res = await handler(postJson({ key: "A:x.js" }));
  assert.equal(res.status, 429);
  assert.deepEqual(await read(res), { status: "too_soon", requested_at: iso });
  assert.equal(calls.length, 5);
});

test("details: resummarize retry failing gives 502", async () => {
  for (const [replies, error] of [
    [[jsonResponse(404, {}), jsonResponse(422, {})], "github 422"],
    [[jsonResponse(500, {})], "github 500"],
  ]) {
    configure();
    const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(201, {}),
      jsonResponse(409, {}), ...replies]);
    const res = await handler(postJson({ key: "A:x.js" }));
    assert.equal(res.status, 502);
    assert.deepEqual(await read(res), { status: "error", error });
    assert.equal(calls.length, 4 + replies.length);
  }
  configure();
  const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(201, {}),
    jsonResponse(500, {})]);
  const res = await handler(postJson({ key: "A:x.js" }));
  assert.equal(res.status, 502);
  assert.deepEqual(await read(res), { status: "error", error: "github 500" });
  assert.equal(calls.length, 4);
});

test("details: entry replaced, old and invalid entries dropped, newest first", async () => {
  configure();
  const fresh = isoMinutesAgo(60 * 24 * 6);
  const newer = isoMinutesAgo(30);
  const calls = fakeFetch([
    jsonResponse(404, {}),
    jsonResponse(200, detailsFile({
      "A:x.js": { requested_at: isoMinutesAgo(20), author: "Old", guidance: "old" },
      "A:old.js": { requested_at: isoMinutesAgo(60 * 24 * 7 + 1), author: "", guidance: "" },
      "A:fresh.js": { requested_at: fresh, author: "F", guidance: "" },
      "A:newer.js": { requested_at: newer, author: "N", guidance: "n" },
      "A:bad.js": { requested_at: "not a date", author: "", guidance: "" },
      "A:str.js": newer,
      "A:null.js": null,
      "A:arr.js": [newer],
      "A:num.js": { requested_at: 5 },
    })),
    jsonResponse(200, {}),
    jsonResponse(201, {}),
  ]);
  const res = await handler(postJson({ key: "A:x.js", author: "Kim", guidance: "new" }));
  assert.equal(res.status, 202);
  const body = await read(res);
  const put = detailsPut(calls[2]);
  assert.equal(put.sha, "ghi");
  assert.deepEqual(put.file, { version: 1, requests: {
    "A:x.js": { requested_at: body.requested_at, author: "Kim", guidance: "new" },
    "A:newer.js": { requested_at: newer, author: "N", guidance: "n" },
    "A:fresh.js": { requested_at: fresh, author: "F", guidance: "" },
  } });
  assert.deepEqual(Object.keys(put.file.requests), ["A:x.js", "A:newer.js", "A:fresh.js"]);
});

test("details: at most 500 newest entries are kept", async () => {
  configure();
  const requests = {};
  for (let i = 0; i < 600; i++) {
    requests["A:f" + i + ".js"] = { requested_at: isoMinutesAgo(11 + i), author: "", guidance: "" };
  }
  const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(200, detailsFile(requests)),
    jsonResponse(201, {}), jsonResponse(201, {})]);
  const res = await handler(postKey("A:x.js"));
  assert.equal(res.status, 202);
  const body = await read(res);
  const kept = detailsPut(calls[2]).file.requests;
  assert.equal(Object.keys(kept).length, 500);
  assert.equal(kept["A:x.js"].requested_at, body.requested_at);
  assert.deepEqual(kept["A:f498.js"], requests["A:f498.js"]);
  assert.ok(!("A:f499.js" in kept));
});

test("details: unparseable details file is overwritten with sha", async () => {
  for (const text of ["not json{", "[1]", '{"version":1,"requests":[]}', "null", '{"requests":"x"}']) {
    configure();
    const file = { sha: "ghi", content: Buffer.from(text).toString("base64") };
    const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(200, file), jsonResponse(201, {}),
      jsonResponse(201, {})]);
    const res = await handler(postJson({ key: "A:x.js", author: "Kim" }));
    assert.equal(res.status, 202, text);
    const body = await read(res);
    const put = detailsPut(calls[2]);
    assert.equal(put.sha, "ghi");
    assert.deepEqual(put.file, { version: 1, requests: {
      "A:x.js": { requested_at: body.requested_at, author: "Kim", guidance: "" } } });
  }
});

test("details: custom REGEN_DETAILS_PATH and branch are used", async () => {
  setEnv({ GITHUB_TOKEN: TOKEN, REBUILD_REPO: "o/r", REBUILD_BRANCH: "b x", REGEN_DETAILS_PATH: "dir/d.json" });
  const url = "https://api.github.com/repos/o/r/contents/dir/d.json";
  const calls = fakeFetch(acceptedReplies());
  const res = await handler(postKey("A:x.js"));
  assert.equal(res.status, 202);
  assert.equal(calls[0].url, RESUMMARIZE_URL + "?ref=b%20x");
  assert.equal(calls[1].url, url + "?ref=b%20x");
  assert.equal(detailsPut(calls[2], url).branch, "b x");
  assert.equal(resummarizePut(calls[3]).branch, "b x");
});

test("details: token never appears in any response", async () => {
  const thrower = (what) => () => { throw new Error(what + " " + TOKEN); };
  const cases = [
    [thrower("a")],
    [jsonResponse(404, {}), thrower("b")],
    [jsonResponse(404, {}), jsonResponse(404, {}), thrower("c")],
    [jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(409, {}), thrower("d")],
    [jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(409, {}), jsonResponse(404, {}), thrower("e")],
    [jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(201, {}), thrower("f")],
    [jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(201, {}), jsonResponse(409, {}), thrower("g")],
    [jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(201, {}), jsonResponse(409, {}),
      jsonResponse(404, {}), thrower("h")],
  ];
  for (const replies of cases) {
    configure();
    const calls = fakeFetch(replies);
    const res = await handler(postJson({ key: "A:x.js", author: "Kim", guidance: "g" }));
    assert.equal(res.status, 502);
    const body = await read(res);
    assert.match(body.error, /^github [a-h] \*\*\*$/);
    assert.equal(calls.length, replies.length);
  }
  for (const replies of [acceptedReplies(), [jsonResponse(404, {}), jsonResponse(500, {})]]) {
    configure();
    fakeFetch(replies);
    await read(await handler(postJson({ key: "A:x.js", author: "Kim" })));
  }
  configure();
  fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}), thrower("i")]);
  const res = await handler(get());
  assert.equal(res.status, 502);
  assert.match((await read(res)).error, /^github i \*\*\*$/);
});

test("GET: regenerate_details keeps only well-formed entries", async () => {
  configure();
  const ok = { requested_at: "2026-10-12T10:00:00Z", author: "Kim", guidance: "a\nb" };
  const calls = fakeFetch([
    jsonResponse(404, {}),
    jsonResponse(404, {}),
    jsonResponse(200, fileBody({ version: 1, requests: {
      "A:ok.js": { ...ok, extra: "dropped" },
      "sandbox|A:ns.js": { requested_at: "2026-10-12T10:01:00Z", author: "", guidance: "" },
      "A:str.js": "2026-10-12T10:00:00Z",
      "A:null.js": null,
      "A:arr.js": [ok],
      "A:noauthor.js": { requested_at: "2026-10-12T10:00:00Z", guidance: "" },
      "A:numauthor.js": { requested_at: "2026-10-12T10:00:00Z", author: 5, guidance: "" },
      "A:noguidance.js": { requested_at: "2026-10-12T10:00:00Z", author: "" },
      "A:nulltime.js": { requested_at: null, author: "", guidance: "" },
    } })),
  ]);
  const res = await handler(get());
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.deepEqual(await read(res), {
    status: "ok",
    rebuild_requested_at: null,
    requests: {},
    regenerate_details: {
      "A:ok.js": ok,
      "sandbox|A:ns.js": { requested_at: "2026-10-12T10:01:00Z", author: "", guidance: "" },
    },
  });
  assert.equal(calls.length, 3);
  assertAllGets(calls);
});

test("GET: malformed details file gives {}", async () => {
  for (const text of ["not json{", "null", "[1]", '{"version":1,"requests":[]}', '{"requests":"x"}', "{}"]) {
    configure();
    fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}),
      jsonResponse(200, { sha: "s", content: Buffer.from(text).toString("base64") })]);
    const res = await handler(get());
    assert.equal(res.status, 200, text);
    assert.deepEqual((await read(res)).regenerate_details, {}, text);
  }
});

test("GET: details file returning 500 gives 502", async () => {
  configure();
  const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(500, {})]);
  const res = await handler(get());
  assert.equal(res.status, 502);
  assert.deepEqual(await read(res), { status: "error", error: "github 500" });
  assert.equal(calls.length, 3);
  assert.equal(calls[2].url, DETAILS_URL + "?ref=rebuild-requests");
  assertAllGets(calls);
});
