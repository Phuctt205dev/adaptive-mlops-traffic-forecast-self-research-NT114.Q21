import test from "node:test";
import assert from "node:assert/strict";
import { modelStatus, progressLabel, freshness } from "../src/trainingMonitorState.js";

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
