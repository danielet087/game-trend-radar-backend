"use strict";

const BRANCH = "radar-notification-state";
const THRESHOLD = 100;
const FAILED = new Set(["failure", "timed_out", "startup_failure", "action_required", "stale"]);

function initialState() {
  return { schema_version: 1, consecutive_failures: 0, alerted: false,
    last_run_id: 0, last_run_attempt: 0, last_outcome: "skipped" };
}

function validateState(state) {
  if (!state || state.schema_version !== 1 ||
      !Number.isSafeInteger(state.consecutive_failures) || state.consecutive_failures < 0 ||
      typeof state.alerted !== "boolean" ||
      !Number.isSafeInteger(state.last_run_id) || state.last_run_id < 0 ||
      !Number.isSafeInteger(state.last_run_attempt) || state.last_run_attempt < 0 ||
      !["success", "failure", "skipped"].includes(state.last_outcome) ||
      state.alerted !== (state.consecutive_failures >= THRESHOLD)) {
    throw new Error("Invalid notification state; refusing to reset the failure count");
  }
  return state;
}

function transition(previous, { runId, runAttempt, outcome, now }) {
  const state = validateState(previous);
  if (!Number.isSafeInteger(runId) || runId < 1 || !Number.isSafeInteger(runAttempt) || runAttempt < 1 ||
      !["success", "failure", "skipped"].includes(outcome)) throw new Error("Invalid run result");
  // Rerunning the notification job cannot count the same run twice or resend
  // its alert. A successful rerun can recover; stale runs cannot erase new work.
  if (outcome === "skipped" || runId < state.last_run_id ||
      (runId === state.last_run_id && runAttempt <= state.last_run_attempt)) {
    return { state, changed: false, alert: false };
  }
  const repeatedFailure = runId === state.last_run_id && state.last_outcome === "failure";
  const count = outcome === "success" ? 0 : state.consecutive_failures + (repeatedFailure ? 0 : 1);
  const alert = outcome === "failure" && count >= THRESHOLD && !state.alerted;
  return { changed: true, alert, state: { ...state,
    consecutive_failures: count,
    alerted: outcome === "success" ? false : state.alerted || alert,
    last_run_id: runId, last_run_attempt: runAttempt, last_outcome: outcome, updated_at: now,
  } };
}

function jobOutcome(jobs, jobName, completionStep) {
  const matches = jobs.filter(job => job.name === jobName);
  if (matches.length !== 1) throw new Error("Monitored job is missing or ambiguous");
  const job = matches[0];
  if (job.status !== "completed") throw new Error("Monitored job has not completed");
  const steps = Array.isArray(job.steps) ? job.steps : [];
  // Inspect real step conclusions as well: the enclosing workflow may be green
  // because its collector job has continue-on-error at JOB level only.
  if (FAILED.has(job.conclusion) || steps.some(step => FAILED.has(step.conclusion))) return "failure";
  if (["cancelled", "skipped", "neutral"].includes(job.conclusion)) return "skipped";
  if (job.conclusion !== "success") throw new Error("Unknown monitored job conclusion");
  return steps.some(step => step.name === completionStep && step.status === "completed" &&
    step.conclusion === "success") ? "success" : "skipped";
}

async function ensureBranch(github, repo) {
  try {
    await github.rest.git.getRef({ ...repo, ref: `heads/${BRANCH}` });
    return;
  } catch (error) { if (error.status !== 404) throw error; }
  // An orphan branch isolates notification metadata from deployment/data
  // commits, database leases, and main-branch protection.
  const { data: tree } = await github.rest.git.createTree({ ...repo, tree: [{
    path: "README.md", mode: "100644", type: "blob",
    content: "# Radar notification state\nPer-workflow counters. Alert once at 100 consecutive failed runs; reset after success.\n",
  }] });
  const { data: commit } = await github.rest.git.createCommit({ ...repo,
    message: "Initialize scheduled failure notification counters", tree: tree.sha, parents: [] });
  try {
    await github.rest.git.createRef({ ...repo, ref: `refs/heads/${BRANCH}`, sha: commit.sha });
  } catch (error) {
    if (![409, 422].includes(error.status)) throw error;
    // Another workflow may have created the same branch concurrently.
    await github.rest.git.getRef({ ...repo, ref: `heads/${BRANCH}` });
  }
}

async function readState(github, repo, path) {
  let data;
  try { ({ data } = await github.rest.repos.getContent({ ...repo, path, ref: BRANCH })); }
  catch (error) {
    if (error.status !== 404) throw error;
    return { state: initialState(), sha: undefined };
  }
  if (data.type !== "file" || data.encoding !== "base64" || typeof data.content !== "string" ||
      !data.sha) throw new Error("Invalid notification state response");
  return { state: validateState(JSON.parse(Buffer.from(data.content, "base64").toString("utf8"))), sha: data.sha };
}

async function saveResult(github, repo, workflowKey, event) {
  await ensureBranch(github, repo);
  const path = `workflows/${workflowKey}.json`;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const previous = await readState(github, repo, path);
    const result = transition(previous.state, event);
    if (!result.changed) return result;
    try {
      await github.rest.repos.createOrUpdateFileContents({ ...repo, path, branch: BRANCH,
        message: `Record ${workflowKey} result for run ${event.runId}`,
        content: Buffer.from(JSON.stringify(result.state, null, 2) + "\n").toString("base64"),
        ...(previous.sha ? { sha: previous.sha } : {}),
      });
      // Never request an alert before its de-duplication marker is durable.
      return result;
    } catch (error) {
      if (![409, 422].includes(error.status) || attempt === 4) throw error;
      // Re-read on compare-and-swap conflicts; never overwrite newer counters.
    }
  }
}

async function record({ github, context, core, workflowKey, jobName, completionStep }) {
  if (!/^[A-Za-z0-9_-]+\.ya?ml$/.test(workflowKey) || !jobName || !completionStep) {
    throw new Error("Invalid scheduled notification configuration");
  }
  const runId = Number(context.runId);
  const runAttempt = Number(process.env.GITHUB_RUN_ATTEMPT || 1);
  const jobs = await github.paginate(github.rest.actions.listJobsForWorkflowRun, {
    ...context.repo, run_id: runId, filter: "latest", per_page: 100,
  });
  const outcome = jobOutcome(jobs, jobName, completionStep);
  const result = await saveResult(github, context.repo, workflowKey, {
    runId, runAttempt, outcome, now: new Date().toISOString(),
  });
  const count = result.state.consecutive_failures;
  core.setOutput("consecutive_failures", String(count));
  core.setOutput("alert", String(result.alert));
  await core.summary.addHeading("雷達排程通知", 3).addRaw(
    `本輪結果：${outcome}；連續失敗：${count}／${THRESHOLD}。\n\n` +
    (result.alert ? "已達通知門檻，本次發出一次失敗通知。" :
      result.state.alerted ? "本段連續失敗已通知，恢復成功前不重複通知。" : "未達通知門檻，不發出失敗通知。") +
    "\n\n收集／發布結果與錯誤仍保留在原工作紀錄；通知成功不代表資料收集完成。\n"
  ).write();
  return result;
}

module.exports = { BRANCH, THRESHOLD, initialState, validateState, transition, jobOutcome, saveResult, record };
