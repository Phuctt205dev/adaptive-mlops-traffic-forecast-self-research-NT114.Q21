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
  const activity = failed
    ? run.state === "cancelling" ? "Stopping the remaining models and removing temporary files." : "This run stopped. Its results will not be published."
    : run.state === "finalizing" ? "Training checks are complete. The system is verifying and saving the results."
      : run.state === "queued" ? "Your request is waiting to start. Updates will appear here automatically."
        : "Models are learning and being checked. Results are saved after all selected models succeed.";

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
      <p className="training-monitor-activity">{activity}</p>
      <div className="training-monitor-summary">
        <div><span>Training now</span><strong>{training.length}</strong><p>{training.map((model) => model.name).join(", ") || "No model is learning right now"}</p></div>
        <div><span>Waiting to start</span><strong>{waiting.length}</strong><p>{waiting.map((model) => model.name).join(", ") || "No models waiting"}</p></div>
        <div><span>Quality checked</span><strong>{evaluated} <small>of {total}</small></strong><p>Selected models that have finished evaluation</p></div>
      </div>
      <div className="training-monitor-progress">
        <div><strong>{evaluated} of {total} models checked</strong><span>{run.state === "finalizing" ? "Saving and checking results" : "Overall progress"}</span></div>
        <progress max={total} value={evaluated} aria-label="Models checked" />
        <p className="training-monitor-progress-note">The bar moves when a model finishes its quality checks. It does not measure time remaining.</p>
      </div>
      <div className="training-monitor-table-wrap">
        <table><thead><tr><th>Model</th><th>Status</th><th>What's happening</th></tr></thead>
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
      <details className="training-monitor-details"><summary>Understand this training</summary>
        <div className="training-monitor-guide">
          <div><span className="training-guide-number">1</span><h4>Learn from past traffic</h4><p>{run.backend === "kaggle" ? "Kaggle trains your selected models. Your system coordinates the work and receives the results." : "Your own system trains the selected models and collects their results."}</p></div>
          <div><span className="training-guide-number">2</span><h4>Check prediction quality</h4><p>A data check tests a model on a later part of historical data. A learning round is one pass through the training data for LSTM or GRU.</p></div>
          <div><span className="training-guide-number">3</span><h4>Save verified results</h4><p>All selected models must succeed before results can be saved. Training a new model does not automatically replace the active model.</p></div>
        </div>
        <details className="training-monitor-identifiers"><summary>Technical references for troubleshooting</summary>
          <dl><dt>Training run</dt><dd>{run.training_run_id}</dd><dt>Airflow run</dt><dd>{run.airflow_dag_run_id || "Not assigned yet"}</dd>
            <dt>Last model update</dt><dd>{timeLabel(run.updated_at, timezone)}</dd></dl>
        </details>
      </details>
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
