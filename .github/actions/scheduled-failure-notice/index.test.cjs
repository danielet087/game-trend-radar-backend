"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { initialState, transition, jobOutcome, saveResult, BRANCH } = require("./index.cjs");
const event = (runId, outcome = "failure", runAttempt = 1) =>
  ({ runId, outcome, runAttempt, now: "2026-10-25T00:00:00Z" });

test("only failure 100 alerts; later failures stay quiet until a real success", () => {
  let state = initialState();
  for (let id = 1; id <= 250; id += 1) {
    const next = transition(state, event(id));
    assert.equal(next.alert, id === 100);
    assert.equal(next.state.consecutive_failures, id);
    state = next.state;
  }
  state = transition(state, event(251, "success")).state;
  assert.equal(state.consecutive_failures, 0);
  assert.equal(state.alerted, false);
  for (let id = 252; id <= 351; id += 1) {
    const next = transition(state, event(id));
    assert.equal(next.alert, id === 351);
    state = next.state;
  }
});

test("duplicate and failed rerun do not increment; a successful rerun recovers", () => {
  let state = transition(initialState(), event(8)).state;
  assert.equal(transition(state, event(8)).changed, false);
  state = transition(state, event(8, "failure", 2)).state;
  assert.equal(state.consecutive_failures, 1);
  state = transition(state, event(8, "success", 3)).state;
  assert.equal(state.consecutive_failures, 0);
});

test("notification-only rerun at the threshold never emits another alert", () => {
  let state = initialState();
  for (let id = 1; id <= 100; id += 1) state = transition(state, event(id)).state;
  const again = transition(state, event(100, "failure", 2));
  assert.equal(again.alert, false);
  assert.equal(again.state.consecutive_failures, 100);
});

test("skipped work and out-of-order old success cannot clear a new failure", () => {
  const state = transition(initialState(), event(10)).state;
  for (const input of [event(11, "skipped"), event(9, "success"), event(9)]) {
    assert.deepEqual(transition(state, input), { state, changed: false, alert: false });
  }
});

test("invalid saved state fails closed instead of discarding the counter", () => {
  for (const bad of [null, {}, { ...initialState(), consecutive_failures: -1 },
    { ...initialState(), consecutive_failures: 100, alerted: false },
    { ...initialState(), consecutive_failures: "99" }]) {
    assert.throws(() => transition(bad, event(20)), /Invalid notification state/);
  }
});

const job = (conclusion, steps = []) => ({ name: "collect", status: "completed", conclusion, steps });
const step = (conclusion, name = "Publish") => ({ name, status: "completed", conclusion });

test("classify real jobs, including failed steps hidden by a green run", () => {
  const classify = value => jobOutcome([value], "collect", "Publish");
  assert.equal(classify(job("failure")), "failure");
  assert.equal(classify(job("timed_out")), "failure");
  assert.equal(classify(job("success", [step("failure")])), "failure");
  assert.equal(classify(job("success", [step("success")])), "success");
  assert.equal(classify(job("success", [step("skipped")])), "skipped");
  assert.equal(classify(job("cancelled")), "skipped");
  assert.equal(classify(job("skipped")), "skipped");
  assert.throws(() => jobOutcome([], "collect", "Publish"), /missing/);
  assert.throws(() => jobOutcome([job("success"), job("failure")], "collect", "Publish"), /ambiguous/);
  assert.throws(() => classify({ ...job("success"), status: "in_progress" }), /not completed/);
});

function fakeGitHub({ conflict = false, unreadable = false, failWrite = false, branchRace = false } = {}) {
  let branch = false, revision = 0, commits = 0;
  const files = new Map();
  const err = status => Object.assign(new Error("test API error"), { status });
  const github = { rest: {
    git: {
      getRef: async () => { if (!branch) throw err(404); return {}; },
      createTree: async ({ tree }) => { assert.equal(tree[0].path, "README.md"); return { data: { sha: "tree" } }; },
      createCommit: async ({ parents }) => { assert.deepEqual(parents, []); commits++; return { data: { sha: "commit" } }; },
      createRef: async ({ ref }) => { assert.equal(ref, `refs/heads/${BRANCH}`); branch = true; if (branchRace) throw err(422); },
    },
    repos: {
      getContent: async ({ path, ref }) => {
        assert.equal(ref, BRANCH);
        if (unreadable) throw err(403);
        if (!files.has(path)) throw err(404);
        return { data: { type: "file", encoding: "base64", ...files.get(path) } };
      },
      createOrUpdateFileContents: async ({ path, branch: ref, sha, content }) => {
        assert.equal(ref, BRANCH);
        if (failWrite) throw err(503);
        if (conflict) { conflict = false; throw err(409); }
        if (sha !== files.get(path)?.sha) throw err(409);
        files.set(path, { sha: String(++revision), content });
      },
    },
  } };
  return { github, files, revisions: () => revision, commits: () => commits };
}
const repo = { owner: "fixture", repo: "radar" };

test("counts persist independently across workflows and API conflicts", async () => {
  const api = fakeGitHub({ conflict: true, branchRace: true });
  for (let id = 1; id <= 100; id += 1) {
    const next = await saveResult(api.github, repo, "growth.yml", event(id));
    assert.equal(next.alert, id === 100);
  }
  const other = await saveResult(api.github, repo, "descriptions.yml", event(101));
  assert.equal(other.state.consecutive_failures, 1);
  const duplicate = await saveResult(api.github, repo, "growth.yml", event(100, "failure", 2));
  assert.equal(duplicate.alert, false);
  assert.equal(duplicate.state.consecutive_failures, 100);
  assert.equal(api.files.size, 2);
  assert.equal(api.commits(), 1);
});

test("read/write failures never authorize an alert or replace state with zero", async () => {
  for (const options of [{ unreadable: true }, { failWrite: true }]) {
    const api = fakeGitHub(options);
    await assert.rejects(() => saveResult(api.github, repo, "growth.yml", event(1)), /test API error/);
    assert.equal(api.revisions(), 0);
  }
});
