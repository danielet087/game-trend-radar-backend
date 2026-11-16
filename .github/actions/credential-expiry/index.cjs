"use strict";

// Records when this repository's pipeline credentials expire, never their
// values. The radar watchdog reads the record from the notification-state
// branch and warns before an expiring credential stops the pipeline.
const { BRANCH, ensureBranch } = require("../scheduled-failure-notice/index.cjs");

const PATH = "credentials/status.json";
const KINDS = ["github", "turso", "cloudflare"];
const NAME = /^[A-Z][A-Z0-9_]{0,63}$/;
const TIMEOUT_MS = 15_000;
const WARN_DAYS = 14;
const DAY = 86_400_000;
const LABELS = { ok: "有效", missing: "未設定", invalid: "失效", unknown: "無法判斷" };

class CheckError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

function isoTime(ms) {
  return new Date(Math.floor(ms / 1000) * 1000).toISOString().replace(".000Z", "Z");
}

function taipei(ms) {
  return new Date(ms + 8 * 3_600_000).toISOString().slice(0, 16).replace("T", " ");
}

function parseSpecs(text) {
  const specs = [];
  for (const line of String(text || "").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const [kind, name, ...extra] = trimmed.split(/\s+/);
    if (!KINDS.includes(kind) || !NAME.test(name || "") || extra.length) {
      throw new Error(`Invalid credential line: ${trimmed}`);
    }
    if (specs.some(spec => spec.name === name)) throw new Error(`Duplicate credential: ${name}`);
    specs.push({ kind, name });
  }
  if (!specs.length) throw new Error("No credentials to check");
  return specs;
}

// GitHub reports a personal access token's expiry on every API response, as
// "2026-12-01 00:00:00 UTC" or with a numeric offset. No header: no expiry.
function githubExpiry(value) {
  if (value === null || value === undefined || !String(value).trim()) return null;
  const match = /^(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)(?:\.\d+)?\s*(UTC|Z|[+-]\d\d:?\d\d)?$/.exec(String(value).trim());
  if (!match) throw new CheckError("expiry_unreadable");
  let zone = match[3] || "Z";
  if (zone === "UTC") zone = "Z";
  else if (zone !== "Z" && !zone.includes(":")) zone = `${zone.slice(0, 3)}:${zone.slice(3)}`;
  const ms = Date.parse(`${match[1]}T${match[2]}${zone}`);
  if (!Number.isFinite(ms)) throw new CheckError("expiry_unreadable");
  return isoTime(ms);
}

async function call(fetchImpl, url, headers) {
  try {
    // Credentials never follow a redirect.
    return await fetchImpl(url, { headers, redirect: "error", signal: AbortSignal.timeout(TIMEOUT_MS) });
  } catch {
    throw new CheckError("network");
  }
}

async function discard(response) {
  try { await response.body?.cancel(); } catch { /* nothing to release */ }
}

async function checkGitHub(token, { fetchImpl }) {
  // rate_limit does not count against the token's API quota.
  const response = await call(fetchImpl, "https://api.github.com/rate_limit", {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "User-Agent": "game-trend-radar-credential-check",
    "X-GitHub-Api-Version": "2026-03-10",
  });
  await discard(response);
  if (response.status === 401) return { status: "invalid", detail: "http_401" };
  if (!response.ok) return { status: "unknown", detail: `http_${response.status}` };
  return { status: "ok", expires_at: githubExpiry(response.headers.get("github-authentication-token-expiration")) };
}

// Turso database tokens are JWTs; an optional exp claim holds the expiry.
async function checkTurso(token) {
  const parts = token.split(".");
  let claims = null;
  if (parts.length === 3) {
    try { claims = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8")); } catch { claims = null; }
  }
  if (!claims || typeof claims !== "object" || Array.isArray(claims)) return { status: "unknown", detail: "not_jwt" };
  if (claims.exp === undefined || claims.exp === null) return { status: "ok", expires_at: null };
  if (!Number.isFinite(claims.exp)) return { status: "unknown", detail: "exp_unreadable" };
  return { status: "ok", expires_at: isoTime(claims.exp * 1000) };
}

// User tokens verify at /user; account-owned tokens only at /accounts/<id>.
async function checkCloudflare(token, { fetchImpl, env }) {
  const account = String(env.CLOUDFLARE_ACCOUNT_ID || "").trim();
  const urls = ["https://api.cloudflare.com/client/v4/user/tokens/verify"];
  if (/^[0-9a-f]{32}$/.test(account)) urls.push(`https://api.cloudflare.com/client/v4/accounts/${account}/tokens/verify`);
  let status = 0;
  for (const url of urls) {
    const response = await call(fetchImpl, url, { Accept: "application/json", Authorization: `Bearer ${token}` });
    status = response.status;
    let body = null;
    try { body = await response.json(); } catch { body = null; }
    const result = body && body.success === true ? body.result : null;
    if (response.ok && result && typeof result.status === "string") {
      const expires = typeof result.expires_on === "string" ? Date.parse(result.expires_on) : NaN;
      const expiresAt = Number.isFinite(expires) ? isoTime(expires) : null;
      if (result.status === "active") return { status: "ok", expires_at: expiresAt };
      return { status: "invalid", expires_at: expiresAt, detail: `status_${result.status.replace(/[^a-z_]/gi, "").slice(0, 20)}` };
    }
  }
  // Without the account endpoint, a rejected user check cannot prove an
  // account-owned token invalid.
  const rejected = [400, 401, 403].includes(status) && urls.length === 2;
  return { status: rejected ? "invalid" : "unknown", detail: `http_${status}` };
}

const CHECKS = { github: checkGitHub, turso: checkTurso, cloudflare: checkCloudflare };

async function checkAll(specs, { env, fetchImpl }) {
  const credentials = [];
  for (const spec of specs) {
    const value = String(env[spec.name] || "").trim();
    let result;
    if (!value) result = { status: "missing" };
    else {
      try { result = await CHECKS[spec.kind](value, { fetchImpl, env }); }
      catch (error) { result = { status: "unknown", detail: error instanceof CheckError ? error.code : "check_failed" }; }
    }
    credentials.push({ name: spec.name, kind: spec.kind, status: result.status,
      expires_at: result.expires_at ?? null, ...(result.detail ? { detail: result.detail } : {}) });
  }
  return credentials;
}

async function saveStatus(github, repo, record) {
  await ensureBranch(github, repo);
  const content = Buffer.from(JSON.stringify(record, null, 2) + "\n").toString("base64");
  for (let attempt = 0; attempt < 5; attempt += 1) {
    let sha;
    try {
      const { data } = await github.rest.repos.getContent({ ...repo, path: PATH, ref: BRANCH });
      if (!data || data.type !== "file" || !data.sha) throw new Error("Invalid credential status response");
      sha = data.sha;
    } catch (error) { if (error.status !== 404) throw error; }
    try {
      await github.rest.repos.createOrUpdateFileContents({ ...repo, path: PATH, branch: BRANCH,
        message: `Record credential expiry check for run ${record.run_id}`, content, ...(sha ? { sha } : {}) });
      return;
    } catch (error) {
      // Another writer moved the branch; re-read and try again.
      if (![409, 422].includes(error.status) || attempt === 4) throw error;
    }
  }
}

function describe(item, now) {
  const at = item.expires_at ? Date.parse(item.expires_at) : NaN;
  if (!Number.isFinite(at)) return item.status === "ok" ? "不會到期" : "—";
  if (at <= now) return `${taipei(at)}（已過期）`;
  return `${taipei(at)}（剩 ${Math.ceil((at - now) / DAY)} 天）`;
}

async function summarize(core, record, now) {
  const rows = [[{ data: "金鑰", header: true }, { data: "類型", header: true },
    { data: "狀態", header: true }, { data: "到期（台灣時間）", header: true }]];
  for (const item of record.credentials) {
    rows.push([item.name, item.kind, LABELS[item.status] + (item.detail ? `（${item.detail}）` : ""), describe(item, now)]);
    const at = item.expires_at ? Date.parse(item.expires_at) : NaN;
    if (item.status === "invalid") core.warning(`${item.name} 已失效（${item.detail || "invalid"}）`);
    else if (Number.isFinite(at) && at - now <= WARN_DAYS * DAY) core.warning(`${item.name} 將在 ${taipei(at)} 到期`);
  }
  await core.summary.addHeading("金鑰到期檢查", 3).addTable(rows)
    .addRaw("只記錄到期日和是否有效，不記錄金鑰內容。雷達看門狗會在到期前 14 天開 issue 提醒。\n").write();
}

async function checkAndRecord({ github, context, core, specs, env = process.env, fetchImpl = fetch, now = Date.now() }) {
  const credentials = await checkAll(parseSpecs(specs), { env, fetchImpl });
  const record = { schema_version: 1, checked_at: isoTime(now), run_id: Number(context.runId) || 0, credentials };
  await saveStatus(github, context.repo, record);
  await summarize(core, record, now);
  return record;
}

module.exports = { PATH, WARN_DAYS, parseSpecs, githubExpiry, checkGitHub, checkTurso, checkCloudflare,
  checkAll, saveStatus, checkAndRecord };
