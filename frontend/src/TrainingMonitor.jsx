import React, { useEffect, useRef, useState } from "react";
import { api } from "./api";

const labels = {
  waiting: "Waiting", running: "Training", evaluated: "Evaluated", failed: "Failed",
  cancelling: "Stopping", cancelled: "Stopped", queued: "Waiting", finalizing: "Saving results",
  preparing: "Preparing data", waiting_cloud: "Waiting for Kaggle", model_started: "Training",
  fold_started: "Cross-validation", epoch_started: "Training", epoch_completed: "Training",
  fold_completed: "Cross-validation", model_evaluated: "Evaluated", saving: "Saving files",
  explaining: "Creating explanation", output_verified: "Output verified", not_started: "Not started",
  model_cv_completed: "Cross-validation complete", final_fit_started: "Fitting selected model",
  stop_requested: "Stop requested",
};

function timeLabel(value, timezone) {
  if (!value) return "—";
  try {
    return new Intl.DateTimeFormat("en-GB", { timeZone: timezone || "Asia/Ho_Chi_Minh", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(value));
  } catch { return "—"; }
}

function logMessage(event) {
  const messages = {
    run_queued: "Training request queued", branch_started: "Worker started this branch",
    branch_ready: "Branch output verified", run_completed: "Results saved",
    run_stopped_and_cleaned: "Training stopped and temporary output cleaned",
    stop_pending: "Waiting for stop and cleanup confirmation",
    training_failed: "Training failed; stopping the remaining branches",
    cloud_unavailable: "Kaggle unavailable; no local fallback was started",
    monitoring_timeout: "Training status could not be confirmed", artifact_invalid: "Output validation failed",
    publication_failed: "Results could not be saved safely", worker_restarted: "Worker restarted before completion",
    cleanup_failed: "Temporary output cleanup is not confirmed",
    dispatch_failed: "Airflow submission was not confirmed; this run is closed to new work",
  };
  const details = event.details || {};
  const parts = [messages[event.code] || labels[event.code] || "Training update"];
  if (details.fold != null) parts.push(`Fold ${details.fold}${details.folds ? `/${details.folds}` : ""}`);
  if (details.epoch != null) parts.push(`Epoch ${details.epoch}${details.epochs ? `/${details.epochs}` : ""}`);
  if (Number.isFinite(details.loss)) parts.push(`Loss ${Number(details.loss).toFixed(5)}`);
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
        <div><h2>{run.dataset}</h2><p>{run.backend === "kaggle" ? "Kaggle" : "Local"} · {labels[run.state] || run.state}</p></div>
        <span className={`training-monitor-state ${failed ? "is-error" : ""}`}>{labels[run.state] || run.state}</span>
      </div>
      {failed && <div className="training-monitor-error" role="alert">
        <strong>{run.error_message || "Training failed. The remaining work is being stopped."}</strong>
        {run.state === "cancelling" && <p>Waiting for confirmation that all work has stopped and temporary output is cleaned.</p>}
        {run.state === "failed" && <button type="button" className="secondary" disabled={dismissing} onClick={dismiss}>Dismiss</button>}
      </div>}
      {dismissError && <p className="training-monitor-error" role="alert">{dismissError}</p>}
      <div className="training-monitor-progress">
        <div><strong>{evaluated}/{models.length} models evaluated</strong><span>{run.state === "finalizing" ? "Saving and verifying results" : "Model evaluation progress"}</span></div>
        <progress max={total} value={evaluated} aria-label="Models evaluated" />
      </div>
      <div className="training-monitor-table-wrap">
        <table><thead><tr><th>Model</th><th>Status</th><th>Progress</th></tr></thead>
          <tbody>{models.map((model) => <tr key={model.id} className={["failed", "cancelling"].includes(model.state) ? "training-monitor-error-row" : ""}>
            <td><strong>{model.name}</strong></td><td>{labels[model.state] || model.state}</td>
            <td>{labels[model.stage] || model.stage}
              {model.fold != null && ` · Fold ${model.fold}${model.folds ? `/${model.folds}` : ""}`}
              {model.epoch != null && ` · Epoch ${model.epoch}${model.epochs ? `/${model.epochs}` : ""}`}</td>
          </tr>)}</tbody>
        </table>
      </div>
      <div className="training-monitor-log-heading"><h3>Training log</h3>
        <label><input type="checkbox" checked={errorsOnly} onChange={(event) => setErrorsOnly(event.target.checked)} /> Errors only</label>
        <label><input type="checkbox" checked={autoScroll} onChange={(event) => setAutoScroll(event.target.checked)} /> Auto-scroll</label>
      </div>
      <div className="training-monitor-logs" ref={logContainer} role="log" aria-label="Training log">
        <table><thead><tr><th>Time</th><th>Model / branch</th><th>Message</th></tr></thead>
          <tbody>{events.map((event) => <tr key={event.sequence} className={event.level === "error" ? "training-monitor-error-row" : ""}>
            <td>{timeLabel(event.time, timezone)}</td><td>{models.find((model) => model.id === event.model)?.name || event.branch || "Run"}</td>
            <td>{logMessage(event)}</td>
          </tr>)}</tbody>
        </table>
      </div>
      <details className="training-monitor-details"><summary>View details</summary>
        <p>Run: {run.training_run_id}</p><p>Airflow: {run.airflow_dag_run_id || "—"}</p>
        <p>Last update: {timeLabel(run.updated_at, timezone)}</p>
        <p>The bar counts evaluated models. Results are saved only after the complete run succeeds.</p>
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
