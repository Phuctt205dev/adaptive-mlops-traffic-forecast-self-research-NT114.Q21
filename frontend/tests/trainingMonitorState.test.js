import test from "node:test";
import assert from "node:assert/strict";
import { modelStatus, progressLabel, freshness, kaggleQueueMinutes, stopWaitNotice, queueWaitNotice, QUEUE_WARNING_MINUTES, gpuFallbackNotices } from "../src/trainingMonitorState.js";

test("Local queue does not make an already finished tree model wait for CPU again", () => {
  const run = { backend: "local", state: "running", branches: { tree: { observation: { phase: "waiting_resources" } } } };
  assert.equal(modelStatus(run, { id: "xgboost_lag", stage: "queued" }), "Waiting for RKE2 resources");
  assert.match(modelStatus(run, { id: "random_forest_lag", stage: "model_evaluated", ml_completed: true }), /^ML finished/);
  assert.equal(modelStatus(run, { id: "random_forest_lag", stage: "output_verified", ml_completed: true }), "Results received and checked");
});

test("unknown Kaggle connection is distinct from failure and cancellation", () => {
  const run = { state: "running", branches: { gru: { observation: { phase: "needs_attention" } } } };
  assert.equal(modelStatus(run, { id: "gru", stage: "queued" }), "Connection needs attention");
  assert.equal(modelStatus({ ...run, state: "cancelling" }, { id: "gru", state: "cancelling" }), "Stopping");
});

test("input reconciliation shows data checks without claiming ML has started", () => {
  const run = { backend: "kaggle", state: "running", branches: { gru: { observation: { phase: "input_reconciling" } } } };
  assert.equal(modelStatus(run, { id: "gru", stage: "queued", state: "waiting" }), "Checking uploaded training data");
  assert.equal(modelStatus(run, { id: "gru", stage: "output_verified", ml_completed: true }), "Results received and checked");
  assert.equal(modelStatus({ ...run, state: "cancelling" }, { id: "gru", state: "cancelling" }), "Stopping");
});

test("notebook submission check is shown as a check, not as a connection failure", () => {
  const run = { backend: "kaggle", state: "running", branches: { lstm: { observation: { phase: "submit_reconciling" } } } };
  assert.equal(modelStatus(run, { id: "lstm", stage: "queued", state: "waiting" }), "Checking that Kaggle received the notebook");
  assert.equal(modelStatus({ ...run, state: "cancelling" }, { id: "lstm", state: "cancelling" }), "Stopping");
});

test("a branch that moved from a queued Kaggle GPU to CPU is reported once per branch", () => {
  const run = { backend: "kaggle", state: "running", branches: {
    tree: { observation: { phase: "branch_ready" } },
    lstm: { observation: { phase: "ml_running", gpu_fallback: { after_seconds: 124, at: "2026-11-27T15:00:00+00:00" } } },
    gru: { observation: { phase: "ml_running" } } } };
  assert.deepEqual(gpuFallbackNotices(run), [{ branch: "lstm", seconds: 124 }]);
  assert.deepEqual(gpuFallbackNotices({ ...run, backend: "local" }), []);
  const switching = { backend: "kaggle", state: "running", branches: { gru: { observation: { phase: "gpu_fallback" } } } };
  assert.equal(modelStatus(switching, { id: "gru", stage: "queued", state: "waiting" }), "No GPU in time · Switching to CPU");
});

test("CV completion is a wait for final training, not completion of the run", () => {
  assert.match(modelStatus({}, { id: "lstm", stage: "model_cv_completed" }), /Waiting for final training/);
});

test("prepared branch and provider queue are distinct from active ML", () => {
  const model = { id: "lstm", stage: "queued" };
  assert.equal(modelStatus({}, model), "Waiting to be scheduled");
  assert.equal(modelStatus({ branches: { lstm: { observation: { phase: "runtime_preparing" } } } }, model), "Preparing training dependencies");
  assert.equal(modelStatus({ branches: { lstm: { observation: { provider_state: "QUEUED" } } } }, model), "Queued on Kaggle");
});

test("full/reduced and final epoch resets carry their own purpose", () => {
  assert.match(progressLabel({ stage: "epoch_started", fold: 2, folds: 3, epoch: 1, epochs: 10, fit_purpose: "cv_reduced" }), /Reduced features · CV 2\/3 · Epoch 1\/10/);
  assert.equal(progressLabel({ stage: "epoch_started", epoch: 1, epochs: 4, fit_purpose: "final" }), "Learning · Final fit · Epoch 1/4");
});

test("early stopping reports actual completed epochs", () => {
  assert.match(progressLabel({ stage: "fit_completed", early_stopped: true, epochs_completed: 3 }), /Stopped early after 3 epochs/);
});

test("finished ML still shows result download and reload verification", () => {
  const run = { branches: { lstm: { observation: { phase: "output_download" } } } };
  assert.equal(modelStatus(run, { id: "lstm", stage: "model_evaluated", ml_completed: true }), "Receiving result files");
  assert.equal(modelStatus(run, { id: "lstm", stage: "output_verified", ml_completed: true }), "Results received and checked");
});

test("upstream failure does not claim the blocked model tried and failed to learn", () => {
  assert.equal(modelStatus({}, { id: "gru", state: "cancelled", blocked_by_branch: "lstm" }), "Not started · Stopped after LSTM failed");
  assert.match(modelStatus({ branches: { lstm: { stop_confirmed: false } } }, { id: "lstm", failure_origin: true }), /Checking stop/);
});

test("fresh heartbeat does not make stale ML progress look fresh", () => {
  assert.match(freshness({ last_ml_received_at: "2026-11-09T00:00:00Z", ml_age_seconds: 300, heartbeat_at: "2026-11-09T00:05:00Z" }), /Last received 300s ago/);
  assert.equal(freshness({}), "No ML progress received yet");
  assert.match(freshness({ last_ml_received_at: "2026-11-09T00:00:00Z", ml_age_seconds: 35 }, 60), /95s ago/);
  assert.equal(freshness({ stage: "model_cv_completed" }), "ML update time unavailable");
});

test("a notebook queued on Kaggle shows how long it has waited", () => {
  const observation = { provider_state: "QUEUED", provider_observed_at: "2026-11-23T06:56:33+00:00" };
  const run = { observed_at: "2026-11-23T07:21:40+00:00", branches: { lstm: { observation } } };
  assert.equal(kaggleQueueMinutes(run, run.branches.lstm), 25);
  assert.equal(modelStatus(run, { id: "lstm", stage: "queued" }), "Queued on Kaggle · 25 min");
  assert.equal(kaggleQueueMinutes(run, { observation: { ...observation, provider_state: "RUNNING" } }), null);
  assert.equal(kaggleQueueMinutes({ observed_at: "not a date" }, { observation }), null);
});

test("stop of a notebook still queued on Kaggle explains how to finish it", () => {
  const attempt = "0a1b2c3d-1111-2222-3333-444455556666";
  const run = { backend: "kaggle", state: "cancelling", cancel_requested: true, branches: {
    tree: { attempt_id: "x", stop_confirmed: true, observation: { provider_state: "CANCEL_ACKNOWLEDGED" } },
    lstm: { attempt_id: attempt, stop_confirmed: false, observation: { provider_state: "QUEUED", stop_waiting_since: "2026-11-23T07:14:48+00:00" } } } };
  assert.deepEqual(stopWaitNotice(run), { branch: "lstm", notebook: "traffic-train-0a1b2c3d111122223333444455556666" });
  assert.equal(stopWaitNotice({ ...run, state: "running" }), null);
  assert.equal(stopWaitNotice({ ...run, backend: "local" }), null);
  const running = { ...run, branches: { lstm: { ...run.branches.lstm, observation: { provider_state: "RUNNING", stop_waiting_since: "x" } } } };
  assert.equal(stopWaitNotice(running), null);
});

test("a Kaggle queue is flagged early, but only for a run that is still going", () => {
  const attempt = "0a1b2c3d-1111-2222-3333-444455556666";
  const queued = (at) => ({ attempt_id: attempt, observation: { provider_state: "QUEUED", provider_observed_at: at } });
  const run = (at, extra = {}) => ({ backend: "kaggle", state: "running", observed_at: "2026-11-23T07:21:40+00:00",
    branches: { tree: { attempt_id: "x", observation: { provider_state: "COMPLETE" } }, lstm: queued(at) }, ...extra });
  assert.equal(QUEUE_WARNING_MINUTES, 5);
  assert.deepEqual(queueWaitNotice(run("2026-11-23T07:06:00+00:00")),
    { branch: "lstm", minutes: 15, notebook: "traffic-train-0a1b2c3d111122223333444455556666" });
  assert.equal(queueWaitNotice(run("2026-11-23T07:17:00+00:00")), null);       // 4 minutes
  assert.equal(queueWaitNotice(run("2026-11-23T07:16:40+00:00")).minutes, 5);  // the system gives up by itself at 15
  assert.equal(queueWaitNotice(run("2026-11-23T06:56:33+00:00", { state: "cancelling" })), null); // Stop notice covers it
  assert.equal(queueWaitNotice(run("2026-11-23T06:56:33+00:00", { state: "completed" })), null);
  assert.equal(queueWaitNotice(run("2026-11-23T06:56:33+00:00", { backend: "local" })), null);
  assert.equal(queueWaitNotice({ ...run("2026-11-23T06:56:33+00:00"), observed_at: "not a date" }), null);
});

test("monitor polls fast only while a run is shown, to save the ngrok request quota", async () => {
  const { monitorPollDelay, MONITOR_POLL_ACTIVE_MS, MONITOR_POLL_IDLE_MS } = await import("../src/trainingMonitorState.js");
  assert.equal(monitorPollDelay([{ training_run_id: "run" }]), MONITOR_POLL_ACTIVE_MS);
  assert.equal(monitorPollDelay([]), MONITOR_POLL_IDLE_MS);
  assert.equal(monitorPollDelay(undefined), MONITOR_POLL_IDLE_MS);
  assert.ok(MONITOR_POLL_ACTIVE_MS >= 5000 && MONITOR_POLL_IDLE_MS >= 4 * MONITOR_POLL_ACTIVE_MS);
  // One hour with a run on screen: 720 requests instead of 1,800 at the former 2-second poll.
  assert.equal(3600 * 1000 / MONITOR_POLL_ACTIVE_MS, 720);
});

test("starting a training run asks the monitor to refresh at once", async () => {
  const { announceTrainingStarted, TRAINING_STARTED_EVENT } = await import("../src/trainingMonitorState.js");
  const target = new EventTarget();
  let heard = 0;
  target.addEventListener(TRAINING_STARTED_EVENT, () => { heard += 1; });
  announceTrainingStarted(target);
  assert.equal(heard, 1);
});
