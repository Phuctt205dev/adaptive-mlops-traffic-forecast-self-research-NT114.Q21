// Display confirmed observations; elapsed time is never inferred from model count.
export const stages = {
  waiting: "Waiting", running: "In progress", evaluated: "ML finished", failed: "Failed",
  cancelling: "Stopping", cancelled: "Stopped", queued: "Waiting to be scheduled",
  finalizing: "Saving results", completed: "Completed", preparing: "Preparing data",
  reconnecting: "Reconnecting to Kaggle", needs_attention: "Needs attention",
  waiting_cloud: "Waiting for Kaggle", model_started: "Starting model checks",
  fold_started: "Checking on past data", fold_completed: "Data check finished",
  epoch_started: "Learning", epoch_completed: "Learning round finished",
  model_cv_completed: "CV finished · Waiting for final training", final_fit_started: "Training the final model",
  fit_started: "Learning", fit_completed: "Fit finished", evaluating: "Evaluating predictions",
  explaining: "Calculating feature importance", model_evaluated: "ML finished · Waiting for results",
  saving: "Writing model files", output_verified: "Results received and checked",
  not_started: "Not started", stop_requested: "Stop requested", ready: "Results checked", skipped: "Not selected",
};

export const providerPhases = {
  waiting_resources: "Waiting for RKE2 resources", local_running: "Training on RKE2",
  reconnecting: "Reconnecting to Kaggle", needs_attention: "Connection needs attention",
  input_reconciling: "Checking uploaded training data",
  submit_reconciling: "Checking that Kaggle received the notebook",
  gpu_fallback: "No GPU in time · Switching to CPU",
  prepare_input: "Preparing input", input_upload: "Uploading input", input_processing: "Kaggle is preparing input",
  notebook_submit: "Submitting to Kaggle", provider_wait: "Submitted · Waiting for Kaggle progress",
  supervisor_started: "Starting the Kaggle environment", child_running: "Starting the training process",
  runtime_preparing: "Preparing training dependencies", runtime_ready: "Training environment ready",
  output_writing: "Writing result files", output_listing: "Waiting for output files",
  output_download: "Receiving result files", output_verification: "Checking result files",
  output_verified: "Result files checked", verify_serving: "Checking model reload and predictions",
  publish_branch: "Saving this group's results", branch_ready: "This group's results are ready",
};

const purposes = { cv_full: "Full features", cv_reduced: "Reduced features", cv_reused: "Reused features", final: "Final fit" };
export function branchFor(model) { return model.id?.endsWith("_lag") ? "tree" : model.id; }

export function progressLabel(model) {
  const parts = [stages[model.stage] || "Waiting for progress"];
  if (model.fit_purpose) parts.push(purposes[model.fit_purpose] || "");
  if (model.fold != null) parts.push(`CV ${model.fold}${model.folds ? `/${model.folds}` : ""}`);
  if (model.epoch != null) parts.push(`Epoch ${model.epoch}${model.epochs ? `/${model.epochs}` : ""}`);
  if (model.early_stopped) parts.push(`Stopped early after ${model.epochs_completed} epochs`);
  return parts.filter(Boolean).join(" · ");
}

// Minutes a branch has waited in Kaggle's queue, from confirmed observation times only.
export function kaggleQueueMinutes(run, branch) {
  const observation = branch?.observation;
  if (observation?.provider_state !== "QUEUED") return null;
  const since = Date.parse(observation.provider_observed_at);
  const now = Date.parse(run?.observed_at);
  if (!Number.isFinite(since) || !Number.isFinite(now) || now < since) return null;
  return Math.floor((now - since) / 60000);
}

// Past this many minutes in Kaggle's queue the Monitor says so; at 15 minutes the run stops by itself.
export const QUEUE_WARNING_MINUTES = 5;

// Kaggle gives no reason or position for a queued notebook, so only the wait is reported.
export function queueWaitNotice(run) {
  if (run?.backend !== "kaggle" || !["running", "queued"].includes(run.state)) return null;
  for (const [branch, item] of Object.entries(run.branches || {})) {
    const minutes = kaggleQueueMinutes(run, item);
    if (item.attempt_id && minutes != null && minutes >= QUEUE_WARNING_MINUTES) {
      return { branch, minutes, notebook: "traffic-train-" + String(item.attempt_id).replace(/-/g, "") };
    }
  }
  return null;
}

// A notebook that is still queued cannot read the stop signal until Kaggle starts it.
// Once the signal's lease has ended it can no longer train, so the stop finishes by itself.
// LSTM/GRU branches that Kaggle did not give a GPU in time and that train on CPU instead.
export function gpuFallbackNotices(run) {
  if (run?.backend !== "kaggle") return [];
  return Object.entries(run.branches || {})
    .filter(([, item]) => item.observation?.gpu_fallback)
    .map(([branch, item]) => ({ branch, seconds: Number(item.observation.gpu_fallback.after_seconds) || 0 }));
}

export function stopWaitNotice(run) {
  if (run?.backend !== "kaggle" || run.state !== "cancelling") return null;
  for (const [branch, item] of Object.entries(run.branches || {})) {
    if (item.stop_confirmed || !item.attempt_id) continue;
    if (item.observation?.provider_state === "QUEUED" && item.observation.stop_waiting_since) {
      return { branch, notebook: "traffic-train-" + String(item.attempt_id).replace(/-/g, "") };
    }
  }
  return null;
}

export function modelStatus(run, model) {
  const branch = run.branches?.[branchFor(model)] || {};
  if (model.failure_origin) return "This group failed · " + (branch.stop_confirmed ? "Stop confirmed" : "Checking stop");
  if (model.blocked_by_branch) return `Not started · Stopped after ${model.blocked_by_branch === "tree" ? "tree group" : model.blocked_by_branch.toUpperCase()} failed`;
  if (["failed", "cancelling", "cancelled"].includes(model.state)) return stages[model.state];
  const phase = branch.observation?.phase;
  if (model.stage === "output_verified") return stages.output_verified;
  if (run.backend === "local" && model.ml_completed && ["waiting_resources", "local_running"].includes(phase)) return stages.model_evaluated;
  if (["reconnecting", "needs_attention", "waiting_resources"].includes(phase) && !model.ml_completed && !["failed", "cancelled", "cancelling"].includes(run.state)) return providerPhases[phase];
  if (model.ml_completed && providerPhases[phase]) return providerPhases[phase];
  if (!model.last_ml_received_at && ["queued", "waiting_cloud", "preparing"].includes(model.stage)) {
    if (branch.observation?.provider_state === "QUEUED") {
      const minutes = kaggleQueueMinutes(run, branch);
      return minutes ? `Queued on Kaggle · ${minutes} min` : "Queued on Kaggle";
    }
    return providerPhases[phase] || (branch.attempt_id ? "Waiting for model progress" : "Waiting to be scheduled");
  }
  return progressLabel(model);
}

export function freshness(model, snapshotAgeSeconds = 0) {
  if (!model.last_ml_received_at) return model.stage && !["queued", "not_started", "preparing", "waiting_cloud"].includes(model.stage)
    ? "ML update time unavailable" : "No ML progress received yet";
  if (model.ml_completed || ["failed", "cancelled"].includes(model.state)) return "Last confirmed ML update";
  const seconds = Math.max(0, Math.floor((Number(model.ml_age_seconds) || 0) + snapshotAgeSeconds));
  return seconds >= 30 ? `Waiting for new ML progress · Last received ${seconds}s ago` : `ML progress received ${seconds}s ago`;
}

// Every monitor request goes through ngrok, whose free plan allows 20,000 requests a month
// (experiments/E4-san-sang-chiu-loi-chiu-tai.md). Poll often only while a run is shown;
// an idle page checks slowly and refreshes at once when this page starts a training run.
export const MONITOR_POLL_ACTIVE_MS = 5000;
export const MONITOR_POLL_IDLE_MS = 20000;
export const TRAINING_STARTED_EVENT = "traffic-training-started";

export function monitorPollDelay(items) {
  return Array.isArray(items) && items.length ? MONITOR_POLL_ACTIVE_MS : MONITOR_POLL_IDLE_MS;
}

export function announceTrainingStarted(target = globalThis) {
  target?.dispatchEvent?.(new Event(TRAINING_STARTED_EVENT));
}
