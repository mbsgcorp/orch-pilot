import { test, afterEach } from "node:test";
import assert from "node:assert/strict";

const mod = await import(new URL("../tools/script_map_site/netlify/functions/rebuild.mjs", import.meta.url));
const handler = mod.default;

const TOKEN = "test-token-XYZ";
const ENV_KEYS = ["GITHUB_TOKEN", "REBUILD_REPO", "REBUILD_BRANCH", "REBUILD_PATH",
  "NOTES_PATH", "RESUMMARIZE_PATH"];
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
  put.file = JSON.parse(Buffer.from(put.content, "base64").toString("utf8"));
  return put;
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
  const calls = fakeFetch([jsonResponse(404, { message: "Not Found" }), jsonResponse(201, {})]);
  const res = await handler(postKey(key));
  assert.equal(res.status, 202);
  const body = await read(res);
  assert.deepEqual(Object.keys(body).sort(), ["key", "requested_at", "status"]);
  assertRequested(body);
  assert.equal(body.key, key);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[0].url, RESUMMARIZE_URL + "?ref=rebuild-requests");
  const put = resummarizePut(calls[1]);
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
    const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(200, {})]);
    const res = await handler(postKey("A:x.js"));
    assert.equal(res.status, 202);
    assert.equal(calls[0].url, "https://api.github.com/repos/o/r/contents/dir/r.json?ref=b%20x");
    assert.equal(calls[1].url, "https://api.github.com/repos/o/r/contents/dir/r.json");
    assert.equal(JSON.parse(calls[1].init.body).branch, "b x");
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
    jsonResponse(200, {}),
  ]);
  const res = await handler(postKey("A:x.js"));
  assert.equal(res.status, 202);
  const body = await read(res);
  const put = resummarizePut(calls[1]);
  assert.equal(put.sha, "def");
  assert.deepEqual(put.file.requests, { "A:x.js": body.requested_at });
});

test("key request: a different key while another is recent gives 202", async () => {
  configure();
  const recent = isoMinutesAgo(2);
  const calls = fakeFetch([
    jsonResponse(200, resummarizeFile({ "A:x.js": recent })),
    jsonResponse(200, {}),
  ]);
  const res = await handler(postKey("sandbox|A:x.js"));
  assert.equal(res.status, 202);
  const body = await read(res);
  assert.equal(body.key, "sandbox|A:x.js");
  const put = resummarizePut(calls[1]);
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
    jsonResponse(201, {}),
  ]);
  const res = await handler(postKey("A:x.js"));
  assert.equal(res.status, 202);
  const body = await read(res);
  assert.deepEqual(resummarizePut(calls[1]).file.requests,
    { "A:fresh.js": fresh, "A:x.js": body.requested_at });
});

test("key request: at most 500 newest entries are kept", async () => {
  configure();
  const requests = {};
  for (let i = 0; i < 600; i++) {
    requests["A:f" + i + ".js"] = isoMinutesAgo(11 + i);
  }
  const calls = fakeFetch([jsonResponse(200, resummarizeFile(requests)), jsonResponse(201, {})]);
  const res = await handler(postKey("A:x.js"));
  assert.equal(res.status, 202);
  const body = await read(res);
  const kept = resummarizePut(calls[1]).file.requests;
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
    const calls = fakeFetch([jsonResponse(200, file), jsonResponse(201, {})]);
    const res = await handler(postKey("A:x.js"));
    assert.equal(res.status, 202, text);
    const body = await read(res);
    const put = resummarizePut(calls[1]);
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

test("key request: PUT returning 409 gives 502", async () => {
  configure();
  const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(409, {})]);
  const res = await handler(postKey("A:x.js"));
  assert.equal(res.status, 502);
  assert.deepEqual(await read(res), { status: "error", error: "github 409" });
  assert.equal(calls.length, 2);
});

test("key request: fetch throwing gives 502, token not leaked", async () => {
  configure();
  for (const replies of [
    [() => { throw new Error("connect failed " + TOKEN); }],
    [jsonResponse(404, {}), () => { throw new Error("put failed " + TOKEN); }],
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
    jsonResponse(404, {}),
  ]);
  const res = await handler(get());
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.deepEqual(await read(res), {
    status: "ok",
    rebuild_requested_at: "2026-10-12T09:58:00Z",
    requests: { "A:x.js": "2026-10-12T10:00:00Z", "sandbox|A:y.js": "2026-10-12T10:01:00Z" },
    notes: {},
  });
  assert.equal(calls.length, 3);
  assert.equal(calls[0].url, CONTENTS_URL + "?ref=rebuild-requests");
  assert.equal(calls[1].url, RESUMMARIZE_URL + "?ref=rebuild-requests");
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
  try {
    const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(404, {})]);
    const res = await handler(get());
    assert.equal(res.status, 200);
    assert.equal(calls[0].url, "https://api.github.com/repos/o/r/contents/dir/f.json?ref=b%20x");
    assert.equal(calls[1].url, "https://api.github.com/repos/o/r/contents/dir/r.json?ref=b%20x");
    assertAllGets(calls);
  } finally {
    delete process.env.RESUMMARIZE_PATH;
  }
});

test("GET: both files missing gives null and {}", async () => {
  configure();
  const calls = fakeFetch([jsonResponse(404, { message: "Not Found" }), jsonResponse(404, {}),
    jsonResponse(404, {})]);
  const res = await handler(get());
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.deepEqual(await read(res), { status: "ok", rebuild_requested_at: null, requests: {}, notes: {} });
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
      jsonResponse(404, {}),
    ]);
    const res = await handler(get());
    assert.equal(res.status, 200, a);
    assert.deepEqual(await read(res),
      { status: "ok", rebuild_requested_at: null, requests: {}, notes: {} }, a);
    assertAllGets(calls);
  }
  configure();
  fakeFetch([jsonResponse(200, { sha: "s" }), jsonResponse(200, { content: 5 }), jsonResponse(404, {})]);
  const res = await handler(get());
  assert.equal(res.status, 200);
  assert.deepEqual(await read(res), { status: "ok", rebuild_requested_at: null, requests: {}, notes: {} });
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

// Correction notes: POST {"action": "add_note" | "clear_note", ...} and GET "notes".

const NOTES_URL = "https://api.github.com/repos/o/r/contents/summary-notes.json";
const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

function postJson(obj) {
  return postBody(JSON.stringify(obj));
}

function addBody(extra = {}) {
  return { action: "add_note", key: "sandbox|RTMH:a.js", text: "It says X, actually Y.",
    author: "Kevin", ...extra };
}

function clearBody(extra = {}) {
  return { action: "clear_note", key: "RTMH:a.js", note_id: "n1", author: "Kevin",
    reason: "Fixed in code.", ...extra };
}

function makeNote(id, extra = {}) {
  return { id, text: "Note " + id, author: "A", created_at: "2026-10-01T00:00:00Z",
    cleared_at: null, cleared_by: null, clear_reason: null, ...extra };
}

function notesFile(notes, sha = "n-sha") {
  return { sha, content: Buffer.from(JSON.stringify({ version: 1, notes })).toString("base64") };
}

function notesPut(call, url = NOTES_URL) {
  assert.equal(call.init.method, "PUT");
  assert.equal(call.url, url);
  const put = JSON.parse(call.init.body);
  put.file = JSON.parse(Buffer.from(put.content, "base64").toString("utf8"));
  return put;
}

// Replies for a successful note save followed by a successful regenerate request.
function savedReplies(notesGet) {
  return [notesGet, jsonResponse(201, {}), jsonResponse(404, {}), jsonResponse(201, {})];
}

test("add_note: no notes file, PUT without sha, regenerate requested", async () => {
  configure();
  const calls = fakeFetch(savedReplies(jsonResponse(404, {})));
  const res = await handler(postJson(addBody({ text: "  It says X,\nactually Y.  ",
    author: "  Kevin Allen " })));
  assert.equal(res.status, 202);
  const body = await read(res);
  assert.deepEqual(Object.keys(body).sort(), ["note", "regenerate", "requested_at", "status"]);
  assert.equal(body.status, "saved");
  assert.equal(body.regenerate, "requested");
  assert.match(body.requested_at, ISO_RE);
  const note = body.note;
  assert.match(note.id, /^[0-9a-f-]{36}$/);
  assert.match(note.created_at, ISO_RE);
  assert.deepEqual(note, {
    id: note.id, text: "It says X,\nactually Y.", author: "Kevin Allen",
    created_at: note.created_at, cleared_at: null, cleared_by: null, clear_reason: null,
  });
  assert.equal(calls.length, 4);
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[0].url, NOTES_URL + "?ref=rebuild-requests");
  const put = notesPut(calls[1]);
  assert.ok(!("sha" in put));
  assert.equal(put.branch, "rebuild-requests");
  assert.equal(put.message, "note added " + note.created_at);
  assert.deepEqual(put.file, { version: 1, notes: { "RTMH:a.js": [note] } });
  assert.equal(calls[2].url, RESUMMARIZE_URL + "?ref=rebuild-requests");
  const rs = resummarizePut(calls[3]);
  assert.deepEqual(rs.file.requests, { "sandbox|RTMH:a.js": body.requested_at });
  for (const call of calls) {
    const h = new Headers(call.init.headers);
    assert.equal(h.get("authorization"), "Bearer " + TOKEN);
    assert.equal(h.get("accept"), "application/vnd.github+json");
    assert.equal(h.get("x-github-api-version"), "2022-11-28");
    assert.equal(h.get("user-agent"), "mbsg-script-map");
  }
});

test("add_note: existing file keeps other ids, PUT with sha, appends", async () => {
  configure();
  const other = { "B:z.js": [makeNote("z1")], "C:junk.js": "not a list" };
  const existing = makeNote("n1");
  const calls = fakeFetch(savedReplies(jsonResponse(200,
    notesFile({ ...other, "RTMH:a.js": [existing] }))));
  const res = await handler(postJson(addBody({ key: "RTMH:a.js" })));
  assert.equal(res.status, 202);
  const body = await read(res);
  const put = notesPut(calls[1]);
  assert.equal(put.sha, "n-sha");
  assert.equal(put.branch, "rebuild-requests");
  assert.deepEqual(put.file, { version: 1, notes: { ...other, "RTMH:a.js": [existing, body.note] } });
  assert.deepEqual(resummarizePut(calls[3]).file.requests, { "RTMH:a.js": body.requested_at });
});

test("add_note: custom NOTES_PATH and branch are used", async () => {
  setEnv({ GITHUB_TOKEN: TOKEN, REBUILD_REPO: "o/r", REBUILD_BRANCH: "b x", NOTES_PATH: "dir/n.json" });
  const calls = fakeFetch(savedReplies(jsonResponse(404, {})));
  const res = await handler(postJson(addBody()));
  assert.equal(res.status, 202);
  await read(res);
  assert.equal(calls[0].url, "https://api.github.com/repos/o/r/contents/dir/n.json?ref=b%20x");
  assert.equal(notesPut(calls[1], "https://api.github.com/repos/o/r/contents/dir/n.json").branch, "b x");
});

test("add_note: unparseable notes file is replaced, PUT keeps sha", async () => {
  for (const text of ["not json{", "[1]", "null", '{"version":2,"notes":{}}',
    '{"version":1,"notes":[]}', '{"version":1}']) {
    configure();
    const file = { sha: "bad-sha", content: Buffer.from(text).toString("base64") };
    const calls = fakeFetch(savedReplies(jsonResponse(200, file)));
    const res = await handler(postJson(addBody()));
    assert.equal(res.status, 202, text);
    const body = await read(res);
    const put = notesPut(calls[1]);
    assert.equal(put.sha, "bad-sha");
    assert.deepEqual(put.file, { version: 1, notes: { "RTMH:a.js": [body.note] } });
  }
});

test("add_note: invalid input gives 400 with no GitHub calls", async () => {
  const bodies = [
    addBody({ key: 5 }),
    addBody({ key: "no-colon" }),
    addBody({ key: "bad ns|A:x.js" }),
    addBody({ key: undefined }),
    addBody({ text: undefined }),
    addBody({ text: 5 }),
    addBody({ text: "" }),
    addBody({ text: "    " }),
    addBody({ text: "x".repeat(501) }),
    addBody({ text: "tab\there" }),
    addBody({ text: "nul\u0000" }),
    addBody({ text: "cr\rhere" }),
    addBody({ author: undefined }),
    addBody({ author: null }),
    addBody({ author: "" }),
    addBody({ author: "   " }),
    addBody({ author: "a".repeat(41) }),
    addBody({ author: "two\nlines" }),
    addBody({ author: "tab\tname" }),
    { action: "delete_note", key: "A:x.js" },
    { action: null, key: "A:x.js" },
    { action: "add_note" },
  ];
  for (const obj of bodies) {
    configure();
    const calls = fakeFetch([]);
    const res = await handler(postJson(obj));
    assert.equal(res.status, 400, JSON.stringify(obj));
    assert.deepEqual(await read(res), { status: "bad_request" });
    assert.equal(calls.length, 0);
  }
});

test("add_note: boundary lengths and unicode are accepted", async () => {
  for (const extra of [
    { text: "x".repeat(500), author: "a".repeat(40) },
    { text: "  " + "y".repeat(500) + "  ", author: " " + "b".repeat(40) + " " },
    { text: "é ✓ 日本", author: "Zoë" },
    { text: "x", author: "a" },
  ]) {
    configure();
    const calls = fakeFetch(savedReplies(jsonResponse(404, {})));
    const res = await handler(postJson(addBody(extra)));
    assert.equal(res.status, 202, JSON.stringify(extra));
    const body = await read(res);
    assert.equal(body.note.text, extra.text.trim());
    assert.equal(body.note.author, extra.author.trim());
    assert.equal(calls.length, 4);
  }
});

test("add_note: 10 active notes gives 409 too_many_notes, no PUT", async () => {
  configure();
  const list = [];
  for (let i = 0; i < 10; i++) {
    list.push(makeNote("a" + i));
  }
  list.push(makeNote("c0", { cleared_at: "2026-10-02T00:00:00Z", cleared_by: "B", clear_reason: "r" }));
  const calls = fakeFetch([jsonResponse(200, notesFile({ "RTMH:a.js": list }))]);
  const res = await handler(postJson(addBody()));
  assert.equal(res.status, 409);
  assert.deepEqual(await read(res), { status: "too_many_notes" });
  assert.equal(calls.length, 1);

  // Nine active notes leave room for one more.
  configure();
  const calls2 = fakeFetch(savedReplies(jsonResponse(200,
    notesFile({ "RTMH:a.js": list.slice(1) }))));
  const res2 = await handler(postJson(addBody()));
  assert.equal(res2.status, 202);
  await read(res2);
  assert.equal(notesPut(calls2[1]).file.notes["RTMH:a.js"].length, 11);
});

test("add_note: a new id when 2000 ids exist gives 413 notes_full", async () => {
  configure();
  const notes = {};
  for (let i = 0; i < 2000; i++) {
    notes["A:f" + i + ".js"] = [];
  }
  const calls = fakeFetch([jsonResponse(200, notesFile(notes))]);
  const res = await handler(postJson(addBody()));
  assert.equal(res.status, 413);
  assert.deepEqual(await read(res), { status: "notes_full" });
  assert.equal(calls.length, 1);

  // An id that already exists can still take notes.
  configure();
  const calls2 = fakeFetch(savedReplies(jsonResponse(200, notesFile(notes))));
  const res2 = await handler(postJson(addBody({ key: "A:f5.js" })));
  assert.equal(res2.status, 202);
  const body = await read(res2);
  const put = notesPut(calls2[1]);
  assert.equal(Object.keys(put.file.notes).length, 2000);
  assert.deepEqual(put.file.notes["A:f5.js"], [body.note]);

  // 1999 ids leave room for one new id.
  configure();
  delete notes["A:f0.js"];
  const calls3 = fakeFetch(savedReplies(jsonResponse(200, notesFile(notes))));
  const res3 = await handler(postJson(addBody()));
  assert.equal(res3.status, 202);
  await read(res3);
  assert.equal(Object.keys(notesPut(calls3[1]).file.notes).length, 2000);
});

test("clear_note: marks the note cleared and requests a regenerate", async () => {
  configure();
  const other = makeNote("n2");
  const calls = fakeFetch(savedReplies(jsonResponse(200,
    notesFile({ "RTMH:a.js": [makeNote("n1"), other] }))));
  const res = await handler(postJson(clearBody({ key: "sandbox|RTMH:a.js",
    author: " Sam ", reason: "  Code changed.  " })));
  assert.equal(res.status, 202);
  const body = await read(res);
  assert.equal(body.status, "saved");
  assert.equal(body.regenerate, "requested");
  assert.match(body.note.cleared_at, ISO_RE);
  assert.deepEqual(body.note, { ...makeNote("n1"), cleared_at: body.note.cleared_at,
    cleared_by: "Sam", clear_reason: "Code changed." });
  const put = notesPut(calls[1]);
  assert.equal(put.sha, "n-sha");
  assert.equal(put.branch, "rebuild-requests");
  assert.equal(put.message, "note cleared " + body.note.cleared_at);
  assert.deepEqual(put.file, { version: 1, notes: { "RTMH:a.js": [body.note, other] } });
  assert.deepEqual(resummarizePut(calls[3]).file.requests,
    { "sandbox|RTMH:a.js": body.requested_at });
});

test("clear_note: invalid input gives 400 with no GitHub calls", async () => {
  const bodies = [
    clearBody({ key: "A:" }),
    clearBody({ key: null }),
    clearBody({ note_id: "" }),
    clearBody({ note_id: "x".repeat(65) }),
    clearBody({ note_id: 5 }),
    clearBody({ note_id: undefined }),
    clearBody({ author: "" }),
    clearBody({ author: "a".repeat(41) }),
    clearBody({ author: "a\nb" }),
    clearBody({ reason: undefined }),
    clearBody({ reason: "" }),
    clearBody({ reason: "   " }),
    clearBody({ reason: "r".repeat(301) }),
    clearBody({ reason: "two\nlines" }),
    clearBody({ reason: "bell\u0007" }),
  ];
  for (const obj of bodies) {
    configure();
    const calls = fakeFetch([]);
    const res = await handler(postJson(obj));
    assert.equal(res.status, 400, JSON.stringify(obj));
    assert.deepEqual(await read(res), { status: "bad_request" });
    assert.equal(calls.length, 0);
  }
  configure();
  const calls = fakeFetch(savedReplies(jsonResponse(200,
    notesFile({ "RTMH:a.js": [makeNote("x".repeat(64))] }))));
  const res = await handler(postJson(clearBody({ note_id: "x".repeat(64), reason: "r".repeat(300) })));
  assert.equal(res.status, 202);
  await read(res);
  assert.equal(calls.length, 4);
});

test("clear_note: unknown note gives 404, already cleared gives 409, no PUT", async () => {
  const cleared = makeNote("n1", { cleared_at: "2026-10-02T00:00:00Z", cleared_by: "B",
    clear_reason: "r" });
  const cases = [
    [jsonResponse(404, {}), 404, "not_found"],
    [jsonResponse(200, notesFile({})), 404, "not_found"],
    [jsonResponse(200, notesFile({ "RTMH:b.js": [makeNote("n1")] })), 404, "not_found"],
    [jsonResponse(200, notesFile({ "RTMH:a.js": [makeNote("n9")] })), 404, "not_found"],
    [jsonResponse(200, notesFile({ "RTMH:a.js": "junk" })), 404, "not_found"],
    [jsonResponse(200, notesFile({ "RTMH:a.js": [cleared] })), 409, "already_cleared"],
  ];
  for (const [reply, status, name] of cases) {
    configure();
    const calls = fakeFetch([reply]);
    const res = await handler(postJson(clearBody()));
    assert.equal(res.status, status, name);
    assert.deepEqual(await read(res), { status: name });
    assert.equal(calls.length, 1);
  }
});

test("history: each id keeps active notes and its 40 most recent cleared", async () => {
  configure();
  const list = [];
  for (let i = 0; i < 40; i++) {
    const day = String(10 + (i % 20)).padStart(2, "0");
    list.push(makeNote("c" + i, { cleared_at: `2026-09-${day}T00:00:${String(i).padStart(2, "0")}Z`,
      cleared_by: "B", clear_reason: "r" }));
  }
  list.splice(5, 0, makeNote("n1"));
  list.push(makeNote("keep"));
  const calls = fakeFetch(savedReplies(jsonResponse(200, notesFile({ "RTMH:a.js": list }))));
  const res = await handler(postJson(clearBody()));
  assert.equal(res.status, 202);
  const body = await read(res);
  const kept = notesPut(calls[1]).file.notes["RTMH:a.js"];
  // 41 cleared now: the oldest cleared (c0, 2026-09-10T00:00:00Z) is dropped.
  assert.equal(kept.length, 41);
  assert.deepEqual(kept.map((n) => n.id),
    list.map((n) => n.id).filter((id) => id !== "c0"));
  assert.deepEqual(kept.find((n) => n.id === "n1"), body.note);
  assert.ok(kept.some((n) => n.id === "keep"));

  // An add also trims a history that is already over the limit.
  configure();
  const over = [];
  for (let i = 0; i < 45; i++) {
    over.push(makeNote("c" + i, { cleared_at: `2026-09-01T00:00:${String(i).padStart(2, "0")}Z`,
      cleared_by: "B", clear_reason: "r" }));
  }
  const calls2 = fakeFetch(savedReplies(jsonResponse(200, notesFile({ "RTMH:a.js": over }))));
  const res2 = await handler(postJson(addBody()));
  assert.equal(res2.status, 202);
  const body2 = await read(res2);
  const kept2 = notesPut(calls2[1]).file.notes["RTMH:a.js"];
  assert.deepEqual(kept2.map((n) => n.id),
    over.slice(5).map((n) => n.id).concat([body2.note.id]));
});

test("write conflict: 409 PUT re-reads, re-applies and succeeds", async () => {
  configure();
  const concurrent = makeNote("other");
  const calls = fakeFetch([
    jsonResponse(404, {}),
    jsonResponse(409, {}),
    jsonResponse(200, notesFile({ "RTMH:a.js": [concurrent] }, "sha-2")),
    jsonResponse(200, {}),
    jsonResponse(404, {}),
    jsonResponse(201, {}),
  ]);
  const res = await handler(postJson(addBody()));
  assert.equal(res.status, 202);
  const body = await read(res);
  assert.equal(body.regenerate, "requested");
  const first = notesPut(calls[1]);
  assert.ok(!("sha" in first));
  assert.deepEqual(first.file.notes, { "RTMH:a.js": [body.note] });
  assert.equal(calls[2].init.method, "GET");
  const second = notesPut(calls[3]);
  assert.equal(second.sha, "sha-2");
  assert.equal(second.branch, "rebuild-requests");
  assert.deepEqual(second.file.notes, { "RTMH:a.js": [concurrent, body.note] });
  assert.equal(calls.length, 6);
});

test("write conflict: 422 twice gives 502, no regenerate", async () => {
  configure();
  const calls = fakeFetch([
    jsonResponse(404, {}),
    jsonResponse(422, {}),
    jsonResponse(404, {}),
    jsonResponse(422, {}),
  ]);
  const res = await handler(postJson(addBody()));
  assert.equal(res.status, 502);
  assert.deepEqual(await read(res), { status: "error", error: "github 422" });
  assert.equal(calls.length, 4);
});

test("write conflict: re-apply may now be rejected", async () => {
  configure();
  const cleared = makeNote("n1", { cleared_at: "2026-10-02T00:00:00Z", cleared_by: "B",
    clear_reason: "r" });
  const calls = fakeFetch([
    jsonResponse(200, notesFile({ "RTMH:a.js": [makeNote("n1")] })),
    jsonResponse(409, {}),
    jsonResponse(200, notesFile({ "RTMH:a.js": [cleared] }, "sha-2")),
  ]);
  const res = await handler(postJson(clearBody()));
  assert.equal(res.status, 409);
  assert.deepEqual(await read(res), { status: "already_cleared" });
  assert.equal(calls.length, 3);
});

test("notes write errors: GET 500 and other PUT failures give 502", async () => {
  for (const [replies, error] of [
    [[jsonResponse(500, {})], "github 500"],
    [[jsonResponse(404, {}), jsonResponse(500, {})], "github 500"],
    [[jsonResponse(404, {}), jsonResponse(403, {})], "github 403"],
    [[jsonResponse(404, {}), jsonResponse(409, {}), jsonResponse(503, {})], "github 503"],
  ]) {
    configure();
    const calls = fakeFetch(replies);
    const res = await handler(postJson(addBody()));
    assert.equal(res.status, 502);
    assert.deepEqual(await read(res), { status: "error", error });
    assert.equal(calls.length, replies.length);
  }
});

test("notes: fetch throwing gives 502 with the token redacted", async () => {
  const withNote = () => jsonResponse(200, notesFile({ "RTMH:a.js": [makeNote("n1")] }));
  for (const replies of [
    [() => { throw new Error("connect failed " + TOKEN); }],
    [withNote, () => { throw new Error("put failed " + TOKEN); }],
  ]) {
    for (const obj of [addBody(), clearBody()]) {
      configure();
      fakeFetch(replies);
      const res = await handler(postJson(obj));
      assert.equal(res.status, 502);
      const body = await read(res);
      assert.equal(body.status, "error");
      assert.match(body.error, /^github (connect|put) failed \*\*\*$/);
    }
  }
});

test("add_note: regenerate already requested maps to already_requested", async () => {
  configure();
  const iso = isoMinutesAgo(3);
  const calls = fakeFetch([
    jsonResponse(404, {}),
    jsonResponse(201, {}),
    jsonResponse(200, resummarizeFile({ "sandbox|RTMH:a.js": iso })),
  ]);
  const res = await handler(postJson(addBody()));
  assert.equal(res.status, 202);
  const body = await read(res);
  assert.equal(body.status, "saved");
  assert.equal(body.regenerate, "already_requested");
  assert.equal(body.requested_at, iso);
  assert.equal(calls.length, 3);
});

test("add_note: regenerate errors map to failed, the note still saved", async () => {
  for (const tail of [
    [jsonResponse(500, {})],
    [jsonResponse(404, {}), jsonResponse(409, {})],
    [() => { throw new Error("rs failed " + TOKEN); }],
    [jsonResponse(404, {}), () => { throw new Error("rs put failed " + TOKEN); }],
  ]) {
    configure();
    const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(201, {}), ...tail]);
    const res = await handler(postJson(addBody()));
    assert.equal(res.status, 202);
    const body = await read(res);
    assert.equal(body.status, "saved");
    assert.equal(body.regenerate, "failed");
    assert.equal(body.requested_at, null);
    assert.equal(body.note.text, "It says X, actually Y.");
    assert.equal(calls.length, 2 + tail.length);
  }
});

test("key request without action is unchanged by notes support", async () => {
  configure();
  const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(201, {})]);
  const res = await handler(postKey("A:x.js"));
  assert.equal(res.status, 202);
  const body = await read(res);
  assert.deepEqual(Object.keys(body).sort(), ["key", "requested_at", "status"]);
  assert.equal(calls[0].url, RESUMMARIZE_URL + "?ref=rebuild-requests");
});

test("GET: notes are read and sanitized", async () => {
  configure();
  const good = makeNote("g1", { extra: "dropped" });
  const clearedNote = makeNote("g2", { cleared_at: "2026-10-03T00:00:00Z", cleared_by: "B",
    clear_reason: "why" });
  const calls = fakeFetch([
    jsonResponse(404, {}),
    jsonResponse(404, {}),
    jsonResponse(200, fileBody({ version: 1, notes: {
      "A:x.js": [good, clearedNote, "junk", null, [1], { id: 5, text: "t", author: "a",
        created_at: "c" }, { id: "m", text: "t", author: "a" },
        makeNote("g3", { cleared_at: 5, cleared_by: ["x"] })],
      "A:empty.js": [],
      "A:bad.js": "not a list",
      "A:obj.js": { id: "x" },
    } })),
  ]);
  const res = await handler(get());
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("cache-control"), "no-store");
  const body = await read(res);
  const { extra, ...goodOut } = good;
  assert.equal(extra, "dropped");
  assert.deepEqual(body.notes, {
    "A:x.js": [goodOut, clearedNote, makeNote("g3")],
    "A:empty.js": [],
  });
  assert.equal(calls.length, 3);
  assert.equal(calls[2].url, NOTES_URL + "?ref=rebuild-requests");
  assertAllGets(calls);
});

test("GET: missing or malformed notes file gives {}", async () => {
  for (const reply of [
    jsonResponse(404, {}),
    jsonResponse(200, { sha: "s", content: Buffer.from("not json{").toString("base64") }),
    jsonResponse(200, fileBody({ version: 2, notes: { "A:x.js": [makeNote("a")] } })),
    jsonResponse(200, fileBody({ version: 1, notes: [] })),
    jsonResponse(200, fileBody([1])),
    jsonResponse(200, { sha: "s" }),
  ]) {
    configure();
    fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}), reply]);
    const res = await handler(get());
    assert.equal(res.status, 200);
    assert.deepEqual((await read(res)).notes, {});
  }
});

test("GET: custom NOTES_PATH is used; notes GitHub errors give 502", async () => {
  setEnv({ GITHUB_TOKEN: TOKEN, REBUILD_REPO: "o/r", NOTES_PATH: "dir/n.json" });
  const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(404, {})]);
  const res = await handler(get());
  assert.equal(res.status, 200);
  await read(res);
  assert.equal(calls[2].url, "https://api.github.com/repos/o/r/contents/dir/n.json?ref=rebuild-requests");

  configure();
  const calls2 = fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}), jsonResponse(500, {})]);
  const res2 = await handler(get());
  assert.equal(res2.status, 502);
  assert.deepEqual(await read(res2), { status: "error", error: "github 500" });
  assert.equal(calls2.length, 3);

  configure();
  fakeFetch([jsonResponse(404, {}), jsonResponse(404, {}),
    () => { throw new Error("notes failed " + TOKEN); }]);
  const res3 = await handler(get());
  assert.equal(res3.status, 502);
  assert.equal((await read(res3)).error, "github notes failed ***");
});
