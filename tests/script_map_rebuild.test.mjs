import { test, afterEach } from "node:test";
import assert from "node:assert/strict";

const mod = await import(new URL("../tools/script_map_site/netlify/functions/rebuild.mjs", import.meta.url));
const handler = mod.default;

const TOKEN = "test-token-XYZ";
const ENV_KEYS = ["GITHUB_TOKEN", "REBUILD_REPO", "REBUILD_BRANCH", "REBUILD_PATH"];
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
  ]);
  const res = await handler(get());
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.deepEqual(await read(res), {
    status: "ok",
    rebuild_requested_at: "2026-10-12T09:58:00Z",
    requests: { "A:x.js": "2026-10-12T10:00:00Z", "sandbox|A:y.js": "2026-10-12T10:01:00Z" },
  });
  assert.equal(calls.length, 2);
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
    const calls = fakeFetch([jsonResponse(404, {}), jsonResponse(404, {})]);
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
  const calls = fakeFetch([jsonResponse(404, { message: "Not Found" }), jsonResponse(404, {})]);
  const res = await handler(get());
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.deepEqual(await read(res), { status: "ok", rebuild_requested_at: null, requests: {} });
  assert.equal(calls.length, 2);
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
    ]);
    const res = await handler(get());
    assert.equal(res.status, 200, a);
    assert.deepEqual(await read(res), { status: "ok", rebuild_requested_at: null, requests: {} }, a);
    assertAllGets(calls);
  }
  configure();
  fakeFetch([jsonResponse(200, { sha: "s" }), jsonResponse(200, { content: 5 })]);
  const res = await handler(get());
  assert.equal(res.status, 200);
  assert.deepEqual(await read(res), { status: "ok", rebuild_requested_at: null, requests: {} });
});

test("GET: non-string request values are dropped", async () => {
  configure();
  fakeFetch([
    jsonResponse(404, {}),
    jsonResponse(200, fileBody({ version: 1, requests: {
      "A:ok.js": "2026-10-12T10:00:00Z", "A:num.js": 5, "A:null.js": null,
      "A:obj.js": { t: "x" }, "A:bad.js": "not a date" } })),
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
