// Display confirmed observations; elapsed time is never inferred from model count.
export const stages = {
  waiting: "Waiting", running: "In progress", evaluated: "ML finished", failed: "Failed",
  cancelling: "Stopping", cancelled: "Stopped", queued: "Waiting to be scheduled",
  finalizing: "Saving results", completed: "Completed", preparing: "Preparing data",
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

export function modelStatus(run, model) {
  const branch = run.branches?.[branchFor(model)] || {};
  if (model.failure_origin) return "This group failed · " + (branch.stop_confirmed ? "Stop confirmed" : "Checking stop");
  if (model.blocked_by_branch) return `Not started · Stopped after ${model.blocked_by_branch === "tree" ? "tree group" : model.blocked_by_branch.toUpperCase()} failed`;
  if (["failed", "cancelling", "cancelled"].includes(model.state)) return stages[model.state];
  const phase = branch.observation?.phase;
  if (model.stage === "output_verified") return stages.output_verified;
  if (model.ml_completed && providerPhases[phase]) return providerPhases[phase];
  if (!model.last_ml_received_at && ["queued", "waiting_cloud", "preparing"].includes(model.stage)) {
    if (branch.observation?.provider_state === "QUEUED") return "Queued on Kaggle";
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
