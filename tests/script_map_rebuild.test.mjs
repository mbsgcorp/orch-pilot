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

test("GET returns 405", async () => {
  configure();
  const calls = fakeFetch([]);
  const res = await handler(new Request("http://localhost/api/rebuild", { method: "GET" }));
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
