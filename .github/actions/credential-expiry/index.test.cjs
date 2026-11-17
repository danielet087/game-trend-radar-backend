"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { PATH, parseSpecs, githubExpiry, checkGitHub, checkTurso, checkCloudflare, checkAll, checkAndRecord } = require("./index.cjs");
const { BRANCH } = require("../scheduled-failure-notice/index.cjs");

const SECRET = "ghp_fixtureSecretNeverStored";
const NOW = Date.parse("2026-11-17T00:00:00Z");
const jwt = claims => ["e30", Buffer.from(JSON.stringify(claims)).toString("base64url"), "sig"].join(".");

function responder(routes) {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    assert.equal(options.redirect, "error");
    assert.ok(options.signal instanceof AbortSignal);
    const route = routes[url];
    if (!route) throw new Error("unexpected url " + url);
    if (route === "network") throw new Error("socket hang up " + options.headers.Authorization);
    return new Response(route.body === undefined ? null : JSON.stringify(route.body),
      { status: route.status || 200, headers: route.headers || {} });
  };
  return { fetchImpl, calls };
}

test("credential lines are strict: known kinds, env-style names, no duplicates", () => {
  assert.deepEqual(parseSpecs("github FRONTEND_REPO_TOKEN\n\n# comment\n turso TURSO_AUTH_TOKEN \n"),
    [{ kind: "github", name: "FRONTEND_REPO_TOKEN" }, { kind: "turso", name: "TURSO_AUTH_TOKEN" }]);
  for (const bad of ["", "aws KEY", "github lower", "github A B", "github $(id)", "github A\ngithub A"]) {
    assert.throws(() => parseSpecs(bad));
  }
});

test("GitHub expiry header formats normalise to UTC; absent means no expiry", () => {
  assert.equal(githubExpiry(null), null);
  assert.equal(githubExpiry(""), null);
  assert.equal(githubExpiry("2026-12-01 00:00:00 UTC"), "2026-12-01T00:00:00Z");
  assert.equal(githubExpiry("2026-12-01 08:00:00 +0800"), "2026-12-01T00:00:00Z");
  assert.equal(githubExpiry("2026-11-30 17:00:00 -07:00"), "2026-12-01T00:00:00Z");
  assert.throws(() => githubExpiry("next tuesday"), /expiry_unreadable/);
});

test("GitHub tokens: expiry from the header, 401 is invalid, other errors are unknown", async () => {
  const url = "https://api.github.com/rate_limit";
  const expiring = responder({ [url]: { body: {}, headers: { "GitHub-Authentication-Token-Expiration": "2026-11-25 00:00:00 UTC" } } });
  assert.deepEqual(await checkGitHub(SECRET, expiring), { status: "ok", expires_at: "2026-11-25T00:00:00Z" });
  assert.equal(expiring.calls[0].options.headers.Authorization, `Bearer ${SECRET}`);
  assert.deepEqual(await checkGitHub(SECRET, responder({ [url]: { body: {} } })), { status: "ok", expires_at: null });
  assert.deepEqual(await checkGitHub(SECRET, responder({ [url]: { status: 401, body: {} } })), { status: "invalid", detail: "http_401" });
  assert.deepEqual(await checkGitHub(SECRET, responder({ [url]: { status: 503 } })), { status: "unknown", detail: "http_503" });
});

test("Turso tokens: exp claim, no exp, and non-JWT values", async () => {
  assert.deepEqual(await checkTurso(jwt({ exp: NOW / 1000 + 86_400 })), { status: "ok", expires_at: "2026-11-18T00:00:00Z" });
  assert.deepEqual(await checkTurso(jwt({ a: "rw" })), { status: "ok", expires_at: null });
  assert.deepEqual(await checkTurso("plain-secret"), { status: "unknown", detail: "not_jwt" });
  assert.deepEqual(await checkTurso(jwt({ exp: "soon" })), { status: "unknown", detail: "exp_unreadable" });
});

test("Cloudflare tokens: user then account verification; rejection needs both", async () => {
  const user = "https://api.cloudflare.com/client/v4/user/tokens/verify";
  const account = "https://api.cloudflare.com/client/v4/accounts/0123456789abcdef0123456789abcdef/tokens/verify";
  const env = { CLOUDFLARE_ACCOUNT_ID: "0123456789abcdef0123456789abcdef" };
  const active = { success: true, result: { id: "x", status: "active", expires_on: "2027-01-01T00:00:00Z" } };
  const rejected = { status: 401, body: { success: false, errors: [{ code: 1000 }] } };
  assert.deepEqual(await checkCloudflare(SECRET, { ...responder({ [user]: { body: active } }), env }),
    { status: "ok", expires_at: "2027-01-01T00:00:00Z" });
  const viaAccount = responder({ [user]: rejected, [account]: { body: { success: true, result: { status: "active" } } } });
  assert.deepEqual(await checkCloudflare(SECRET, { ...viaAccount, env }), { status: "ok", expires_at: null });
  assert.equal(viaAccount.calls.length, 2);
  assert.deepEqual(await checkCloudflare(SECRET, { ...responder({ [user]: rejected, [account]: rejected }), env }),
    { status: "invalid", detail: "http_401" });
  assert.deepEqual(await checkCloudflare(SECRET, { ...responder({ [user]: rejected }), env: {} }),
    { status: "unknown", detail: "http_401" });
  assert.deepEqual(await checkCloudflare(SECRET, { ...responder({ [user]: { body: { success: true, result: { status: "expired" } } } }), env }),
    { status: "invalid", expires_at: null, detail: "status_expired" });
});

test("missing secrets and network failures are recorded without the secret", async () => {
  const result = await checkAll(parseSpecs("github FRONTEND_REPO_TOKEN\ngithub CONTENT_BACKEND_TOKEN"),
    { env: { FRONTEND_REPO_TOKEN: SECRET }, fetchImpl: responder({ "https://api.github.com/rate_limit": "network" }).fetchImpl });
  assert.deepEqual(result, [
    { name: "FRONTEND_REPO_TOKEN", kind: "github", status: "unknown", expires_at: null, detail: "network" },
    { name: "CONTENT_BACKEND_TOKEN", kind: "github", status: "missing", expires_at: null },
  ]);
  assert.ok(!JSON.stringify(result).includes(SECRET));
});

function fakeGitHub({ conflicts = 0 } = {}) {
  let branch = false;
  const files = new Map();
  const err = status => Object.assign(new Error("test API error"), { status });
  return { files, github: { rest: {
    git: {
      getRef: async () => { if (!branch) throw err(404); return {}; },
      createTree: async () => ({ data: { sha: "tree" } }),
      createCommit: async () => ({ data: { sha: "commit" } }),
      createRef: async () => { branch = true; },
    },
    repos: {
      getContent: async ({ path, ref }) => {
        assert.equal(ref, BRANCH);
        if (!files.has(path)) throw err(404);
        return { data: { type: "file", sha: files.get(path).sha } };
      },
      createOrUpdateFileContents: async ({ path, branch: ref, sha, content }) => {
        assert.equal(ref, BRANCH);
        if (conflicts > 0) { conflicts -= 1; throw err(409); }
        if (sha !== files.get(path)?.sha) throw err(409);
        files.set(path, { sha: String(Math.random()), content });
      },
    },
  } } };
}

function fakeCore() {
  const warnings = [];
  const summary = { addHeading: () => summary, addTable: () => summary, addRaw: () => summary, write: async () => summary };
  return { warnings, summary, warning: message => warnings.push(message) };
}

test("the record lands on the state branch, overwrites itself and warns 14 days ahead", async () => {
  const api = fakeGitHub({ conflicts: 1 });
  const core = fakeCore();
  const env = { FRONTEND_REPO_TOKEN: SECRET, TURSO_AUTH_TOKEN: jwt({ exp: NOW / 1000 + 10 * 86_400 }) };
  const fetchImpl = responder({ "https://api.github.com/rate_limit": { body: {} } }).fetchImpl;
  const specs = "github FRONTEND_REPO_TOKEN\nturso TURSO_AUTH_TOKEN";
  const context = { runId: 42, repo: { owner: "fixture", repo: "radar" } };
  await checkAndRecord({ github: api.github, context, core, specs, env, fetchImpl, now: NOW });
  await checkAndRecord({ github: api.github, context: { ...context, runId: 43 }, core, specs, env, fetchImpl, now: NOW + 1000 });
  assert.deepEqual([...api.files.keys()], [PATH]);
  const saved = JSON.parse(Buffer.from(api.files.get(PATH).content, "base64").toString("utf8"));
  assert.equal(saved.run_id, 43);
  assert.equal(saved.checked_at, "2026-11-17T00:00:01Z");
  assert.deepEqual(saved.credentials.map(item => [item.name, item.status, item.expires_at]),
    [["FRONTEND_REPO_TOKEN", "ok", null], ["TURSO_AUTH_TOKEN", "ok", "2026-11-27T00:00:00Z"]]);
  assert.ok(!api.files.get(PATH).content.includes(Buffer.from(SECRET).toString("base64")));
  assert.ok(!JSON.stringify(saved).includes(SECRET));
  assert.equal(core.warnings.length, 2);
  assert.match(core.warnings[0], /TURSO_AUTH_TOKEN 將在 2026-11-27 08:00 到期/);
});
