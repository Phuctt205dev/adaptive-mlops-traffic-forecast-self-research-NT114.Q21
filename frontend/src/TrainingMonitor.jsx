import React, { useEffect, useRef, useState } from "react";
import { api } from "./api";

const labels = {
  waiting: "Waiting", running: "Training", evaluated: "Checked", failed: "Failed",
  cancelling: "Stopping", cancelled: "Stopped", queued: "Waiting", finalizing: "Saving results",
  preparing: "Preparing data", waiting_cloud: "Waiting for Kaggle", model_started: "Training",
  fold_started: "Checking on past data", epoch_started: "Learning from data", epoch_completed: "Learning round finished",
  fold_completed: "Data check finished", model_evaluated: "Quality checked", saving: "Saving model files",
  explaining: "Finding what affects predictions", output_verified: "Results received and checked", not_started: "Not started",
  model_cv_completed: "Past-data checks finished", final_fit_started: "Training the final model",
  stop_requested: "Stopping requested", ready: "Results checked", skipped: "Not selected", completed: "Completed",
};

const errorMessages = {
  training_failed: "A model could not finish training. This run cannot be saved.",
  cloud_unavailable: "The system could not finish this Kaggle training request. Local training was not started.",
  monitoring_timeout: "The system could not confirm whether training is still running.",
  artifact_invalid: "The returned model files did not pass safety checks and cannot be saved.",
  publication_failed: "The system could not safely save this run's results.",
  worker_restarted: "A training service restarted before the results could be confirmed.",
  cleanup_failed: "The system is still checking that temporary training files have been removed.",
  dispatch_failed: "The training request could not be confirmed. No more models will start in this run.",
};

const branchNames = { tree: "Tree models", lstm: "LSTM", gru: "GRU" };

function progressLabel(model) {
  const parts = [labels[model.stage] || "Waiting for an update"];
  if (model.fold != null) parts.push(`Data check ${model.fold}${model.folds ? ` of ${model.folds}` : ""}`);
  if (model.epoch != null) parts.push(`Learning round ${model.epoch}${model.epochs ? ` of ${model.epochs}` : ""}`);
  return parts.join(" · ");
}

function timeLabel(value, timezone) {
  if (!value) return "—";
  try {
    return new Intl.DateTimeFormat("en-GB", { timeZone: timezone || "Asia/Ho_Chi_Minh", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
  } catch { return "—"; }
}

function logMessage(event) {
  const messages = {
    run_queued: "Your training request is waiting to start", branch_started: "The training service has started this group",
    branch_ready: "This group's results have been received and checked", run_completed: "Training results saved",
    run_stopped_and_cleaned: "Training has stopped and temporary files have been removed",
    stop_pending: "Checking that training has stopped and temporary files are removed",
    training_failed: "Training failed. The other models in this run are being stopped",
    cloud_unavailable: "The system could not finish this Kaggle training request. Local training was not started",
    monitoring_timeout: "The system could not confirm the training status", artifact_invalid: "Returned model files did not pass checks",
    publication_failed: "Results could not be saved safely", worker_restarted: "A training service restarted before completion",
    cleanup_failed: "Removal of temporary files has not been confirmed",
    dispatch_failed: "The training request could not be confirmed. No more models will start in this run",
  };
  const details = event.details || {};
  const parts = [messages[event.code] || labels[event.code] || "Training update"];
  if (details.fold != null) parts.push(`Data check ${details.fold}${details.folds ? ` of ${details.folds}` : ""}`);
  if (details.epoch != null) parts.push(`Learning round ${details.epoch}${details.epochs ? ` of ${details.epochs}` : ""}`);
  return parts.join(" · ");
}

function RunCard({ run, timezone, onDismiss }) {
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const [dismissing, setDismissing] = useState(false);
  const [dismissError, setDismissError] = useState("");
  const logContainer = useRef(null);
  const models = Array.isArray(run.models) ? run.models : [];
  const events = (Array.isArray(run.events) ? run.events : []).filter((event) => !errorsOnly || event.level === "error");
  const total = Math.max(1, Number(run.total_models) || models.length);
  const evaluated = Math.max(0, Math.min(total, Number(run.evaluated_models) || 0));
  const failed = Boolean(run.error_code) || ["failed", "cancelling"].includes(run.state);
  const training = models.filter((model) => model.state === "running");
  const waiting = models.filter((model) => ["waiting", "queued"].includes(model.state));

  useEffect(() => {
    if (autoScroll && logContainer.current) logContainer.current.scrollTop = logContainer.current.scrollHeight;
  }, [run.sequence, autoScroll, errorsOnly]);

  async function dismiss() {
    setDismissing(true); setDismissError("");
    try { await api.dismissTrainingError(run.training_run_id); onDismiss(run.training_run_id); }
    catch { setDismissError("Could not dismiss this error. Stop or cleanup may still be pending."); }
    finally { setDismissing(false); }
  }

  return (
    <article className={`training-monitor-card ${failed ? "training-monitor-has-error" : ""}`}>
      <div className="training-monitor-heading">
        <div><h2>{run.dataset}</h2><p>{run.backend === "kaggle" ? "Training on Kaggle" : "Training on this system"} · Updated {timeLabel(run.updated_at, timezone)}</p></div>
        <span className={`training-monitor-state ${failed ? "is-error" : ""}`}>{labels[run.state] || run.state}</span>
      </div>
      {failed && <div className="training-monitor-error" role="alert">
        <strong>{errorMessages[run.error_code] || run.error_message || "Training failed. The remaining models are being stopped."}</strong>
        {run.state === "cancelling" && <p>Waiting for confirmation that all training has stopped and temporary files have been removed.</p>}
        {run.state === "failed" && <button type="button" className="secondary" disabled={dismissing} onClick={dismiss}>Dismiss</button>}
      </div>}
      {dismissError && <p className="training-monitor-error" role="alert">{dismissError}</p>}
      <div className="training-monitor-summary">
        <div><span>Training now</span><strong>{training.length}</strong><p>{training.map((model) => model.name).join(", ") || "No model is learning right now"}</p></div>
        <div><span>Waiting to start</span><strong>{waiting.length}</strong><p>{waiting.map((model) => model.name).join(", ") || "No models waiting"}</p></div>
        <div><span>Quality checked</span><strong>{evaluated} <small>of {total}</small></strong><p>Selected models that have finished evaluation</p></div>
      </div>
      <div className="training-monitor-progress">
        <div><strong>{evaluated} of {total} models checked</strong><span>{run.state === "finalizing" ? "Saving and checking results" : "Overall progress"}</span></div>
        <progress max={total} value={evaluated} aria-label="Models checked" />
      </div>
      <div className="training-monitor-table-wrap">
        <table><thead><tr><th>Model</th><th>Status</th><th>Progress</th></tr></thead>
          <tbody>{models.map((model) => <tr key={model.id} className={["failed", "cancelling"].includes(model.state) ? "training-monitor-error-row" : ""}>
            <td><strong>{model.name}</strong></td><td><span className={`training-model-state is-${model.state}`}>{labels[model.state] || "Updating"}</span></td>
            <td>{progressLabel(model)}</td>
          </tr>)}</tbody>
        </table>
      </div>
      <div className="training-monitor-log-heading"><h3>Recent activity</h3>
        <label><input type="checkbox" checked={errorsOnly} onChange={(event) => setErrorsOnly(event.target.checked)} /> Errors only</label>
        <label><input type="checkbox" checked={autoScroll} onChange={(event) => setAutoScroll(event.target.checked)} /> Auto-scroll</label>
      </div>
      <div className="training-monitor-logs" ref={logContainer} role="log" aria-label="Recent training activity" tabIndex={0}>
        <table><thead><tr><th>Time</th><th>Model / group</th><th>What happened</th></tr></thead>
          <tbody>{events.length === 0 && <tr><td colSpan={3}>{errorsOnly ? "No errors in the received activity." : "Waiting for the first training update."}</td></tr>}
            {events.map((event) => <tr key={event.sequence} className={event.level === "error" ? "training-monitor-error-row" : ""}>
            <td>{timeLabel(event.time, timezone)}</td><td>{models.find((model) => model.id === event.model)?.name || branchNames[event.branch] || "All models"}</td>
            <td>{logMessage(event)}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </article>
  );
}

export default function TrainingMonitor({ region, enabled }) {
  const [items, setItems] = useState([]);
  const [error, setError] = useState("");
  const [receivedAt, setReceivedAt] = useState(null);

  useEffect(() => {
    setItems([]); setError(""); setReceivedAt(null);
    if (!enabled || !region?.id) return undefined;
    let disposed = false;
    let timer;
    let controller;
    async function refresh() {
      if (disposed || document.hidden) return;
      controller?.abort();
      const request = new AbortController();
      controller = request;
      try {
        const result = await api.trainingMonitor(region.id, { signal: request.signal });
        if (disposed || request.signal.aborted) return;
        setItems(Array.isArray(result.items) ? result.items : []);
        setError(""); setReceivedAt(new Date().toISOString());
      } catch (failure) {
        if (!disposed && !request.signal.aborted && failure.name !== "AbortError") setError("Training updates are unavailable. The last known state is shown.");
      } finally {
        if (!disposed && controller === request && !document.hidden) timer = window.setTimeout(refresh, 5000);
      }
    }
    function visibility() {
      window.clearTimeout(timer);
      if (document.hidden) controller?.abort(); else refresh();
    }
    refresh(); document.addEventListener("visibilitychange", visibility);
    return () => { disposed = true; window.clearTimeout(timer); controller?.abort(); document.removeEventListener("visibilitychange", visibility); };
  }, [enabled, region?.id]);

  // A successful run is omitted by the API. No retained 100% card, log,
  // historical run or empty-state message remains on this screen.
  if (!items.length && !error) return null;
  return <div className="training-monitor">
    {error && <div className="training-monitor-error" role="alert">{error}{receivedAt && ` Last received: ${timeLabel(receivedAt, region?.timezone)}.`}</div>}
    {items.map((run) => <RunCard key={run.training_run_id} run={run} timezone={region?.timezone}
      onDismiss={(id) => setItems((current) => current.filter((item) => item.training_run_id !== id))} />)}
  </div>;
}
