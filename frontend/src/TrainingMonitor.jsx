import React, { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { stages as labels, modelStatus, progressLabel, freshness } from "./trainingMonitorState";
import AppDialog from "./AppDialog.jsx";


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
    publication_started: "All group results checked · Saving the run",
    dispatch_failed: "The training request could not be confirmed. No more models will start in this run",
  };
  const details = event.details || {};
  return messages[event.code] || progressLabel({ stage: event.code, ...details });
}

function RunCard({ run, timezone, onDismiss, snapshotAgeSeconds, capabilities }) {
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const [dismissing, setDismissing] = useState(false);
  const [dismissError, setDismissError] = useState("");
  const [action, setAction] = useState(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [stopRequested, setStopRequested] = useState(false);
  const actionLock = useRef(false);
  const actionRequest = useRef(null);
  const logContainer = useRef(null);
  const models = Array.isArray(run.models) ? run.models : [];
  const events = (Array.isArray(run.events) ? run.events : []).filter((event) => !errorsOnly || event.level === "error");
  const total = Math.max(1, Number(run.total_models) || models.length);
  const evaluated = Math.max(0, Math.min(total, Number(run.evaluated_models) || 0));
  const failed = (Boolean(run.error_code) && run.error_code !== "admin_cancelled") || run.state === "failed";
  const terminal = ["failed", "cancelled", "completed"].includes(run.state);
  const phases = Object.values(run.branches || {}).map((b) => b.observation?.phase);
  const connection = phases.includes("needs_attention") ? "needs_attention" : phases.includes("reconnecting") ? "reconnecting" : null;
  const cvCompleted = Number(run.cv_completed_models) || 0;
  const outputVerified = Number(run.output_verified_models) || 0;

  useEffect(() => {
    if (autoScroll && logContainer.current) logContainer.current.scrollTop = logContainer.current.scrollHeight;
  }, [run.sequence, autoScroll, errorsOnly]);

  async function dismiss() {
    setDismissing(true); setDismissError("");
    try { await api.dismissTrainingError(run.training_run_id); onDismiss(run.training_run_id); }
    catch { setDismissError("Could not dismiss this error. Stop or cleanup may still be pending."); }
    finally { setDismissing(false); }
  }

  async function confirmAction() {
    if (actionLock.current) return;
    actionLock.current = true; setActionBusy(true); setActionError("");
    try {
      if (action === "stop") {
        await api.cancelTrainingRun(run.training_run_id);
        setStopRequested(true);
      } else {
        if (!actionRequest.current) actionRequest.current = crypto.randomUUID();
        await api.retryTrainingRun(run.training_run_id, action, actionRequest.current);
        // A new run already exists even if hiding the old card fails.
        try { await api.dismissTrainingError(run.training_run_id); onDismiss(run.training_run_id); } catch {}
      }
      setAction(null);
      actionRequest.current = null;
    } catch (err) { setActionError(err.message || "The action could not be confirmed. Check the current run before trying again."); }
    finally { actionLock.current = false; setActionBusy(false); }
  }

  return (
    <article className={`training-monitor-card ${failed ? "training-monitor-has-error" : ""}`}>
      <div className="training-monitor-heading">
        <div><h2>{run.dataset}</h2><p>Training on {run.backend === "local" ? "Local · RKE2 CPU" : "Kaggle"} · Snapshot {timeLabel(run.observed_at || run.updated_at, timezone)}</p>
          {run.retry_of && <small>Retry of an earlier run · The original history is preserved</small>}</div>
        <span className={`training-monitor-state ${failed ? "is-error" : ""}`}>{labels[!run.cancel_requested && connection ? connection : run.state] || run.state}</span>
      </div>
      {failed && <div className="training-monitor-error" role="alert">
        <strong>{errorMessages[run.error_code] || run.error_message || "Training failed. The remaining models are being stopped."}</strong>
        {run.state === "cancelling" && <p>Waiting for confirmation that all training has stopped and temporary files have been removed.</p>}
        {run.state === "failed" && <button type="button" className="secondary" disabled={dismissing} onClick={dismiss}>Dismiss</button>}
      </div>}
      {dismissError && <p className="training-monitor-error" role="alert">{dismissError}</p>}
      {connection && !run.cancel_requested && <div className="training-location-note" role="status">
        {connection === "reconnecting" ? "Contact with Kaggle was interrupted. The last confirmed progress is shown; no duplicate training was submitted."
          : "Kaggle's outcome could not be confirmed. Stop this run and wait for stop confirmation before starting a replacement. No Local fallback has started."}
      </div>}
      {run.error_code === "admin_cancelled" && <p role="status">{run.state === "cancelled"
        ? "Cancelled by the administrator. Training has stopped and cleanup is complete."
        : "Stop requested. Waiting for the training processes to stop and cleanup to finish."}</p>}
      <div className="training-monitor-actions">
        {!terminal && <button type="button" className="secondary" disabled={run.cancel_requested || stopRequested}
          onClick={() => { setActionError(""); setAction("stop"); }}>{run.cancel_requested || stopRequested ? "Stopping…" : "Stop training"}</button>}
        {run.can_retry && <>
          {capabilities?.cloud_training && <button type="button" className="secondary" onClick={() => { setActionError(""); setAction("kaggle"); }}>Retry on Kaggle</button>}
          {run.purpose !== "retrain" && capabilities?.local_training && <button type="button" className="secondary" onClick={() => { setActionError(""); setAction("local"); }}>{run.backend === "local" ? "Retry on Local" : "Train again on Local"}</button>}
        </>}
        {run.state === "cancelled" && <button type="button" className="secondary" disabled={dismissing} onClick={dismiss}>Dismiss</button>}
      </div>
      {action && <AppDialog title={action === "stop" ? "Stop this training run?" : `Start a new run on ${action === "local" ? "Local" : "Kaggle"}?`}
        description={action === "stop" ? "All remaining models in this run will be stopped. The model currently serving predictions will continue to work."
          : "A new run will use the original dataset, time window, model selection and configuration. The original run remains in history."}
        onClose={() => { if (!actionBusy) setAction(null); }} busy={actionBusy}
        footer={<><button type="button" className="secondary" disabled={actionBusy} onClick={() => setAction(null)}>Keep current state</button>
          <button type="button" disabled={actionBusy} onClick={confirmAction}>{actionBusy ? "Sending request…" : action === "stop" ? "Stop training" : "Start new run"}</button></>}>
        <strong>{run.dataset}</strong>
        {actionError && <p className="alert alert-error" role="alert">{actionError}</p>}
      </AppDialog>}
      <div className="training-monitor-summary">
        <div><span>CV finished</span><strong>{cvCompleted} <small>of {total}</small></strong><p>Past-data checks completed</p></div>
        <div><span>ML finished</span><strong>{evaluated} <small>of {total}</small></strong><p>Final training and evaluation finished</p></div>
        <div><span>Results checked</span><strong>{outputVerified} <small>of {total}</small></strong><p>Returned files received and verified</p></div>
      </div>
      <div className="training-monitor-progress">
        <div><strong>{run.state === "finalizing" ? "Saving the complete run" : "Confirmed training milestones"}</strong></div>
        <p>Each model has its own stages. Counts show completed milestones, not remaining time.</p>
      </div>
      <div className="training-monitor-table-wrap">
        <table><thead><tr><th>Model</th><th>Current stage</th><th>Last ML update</th></tr></thead>
          <tbody>{models.map((model) => <tr key={model.id} className={["failed", "cancelling"].includes(model.state) ? "training-monitor-error-row" : ""}>
            <td><strong>{model.name}</strong></td><td><span className={`training-model-state is-${model.state}`}>{modelStatus(run, model)}</span></td>
            <td>{freshness(model, snapshotAgeSeconds)}{model.last_ml_received_at && <div>{timeLabel(model.last_ml_received_at, timezone)}</div>}</td>
          </tr>)}</tbody>
        </table>
      </div>
      <details className="training-monitor-details"><summary>Timing and connection details</summary>
        <p>ML timestamps come from the training process. Received timestamps come from this system. Missing measurements remain unknown.</p>
        {Object.entries(run.branches || {}).map(([branch, value]) => <div key={branch}>
          <h4>{branchNames[branch]}</h4>
          <p>Airflow heartbeat: {timeLabel(value.heartbeat_at, timezone)} · Last successful provider check: {timeLabel(value.observation?.provider_checked_at, timezone)} · Observed provider state: {value.observation?.provider_state || "Unknown"}</p>
          {value.observation?.last_failure && <p>Last provider issue: {value.observation.last_failure.diagnostic?.reason || value.observation.last_failure.category}</p>}
          <p>Stop: {!run.cancel_requested ? "not requested" : value.stop_confirmed ? "confirmed" : value.attempt_id ? "not confirmed" : "not started"} · Run staging cleanup: {value.cleanup_confirmed ? "confirmed" : "not confirmed"}</p>
          <table><thead><tr><th>Operation</th><th>Calls</th><th>Total seconds</th><th>Bytes attempted</th></tr></thead><tbody>
            {Object.entries(value.observation?.telemetry?.operations || {}).map(([name, measure]) => <tr key={name}><td>{name.replaceAll("_", " ")}</td><td>{measure.count}</td><td>{Number(measure.seconds).toFixed(2)}</td><td>{measure.bytes || "—"}</td></tr>)}
          </tbody></table>
        </div>)}
        <p>Operation totals can overlap; they are not the total run duration.</p>
        {models.map((model) => <div key={model.id}><h4>{model.name}</h4>
          <p>Last emitted: {timeLabel(model.source_time, timezone)} · Supervisor relay: {timeLabel(model.supervisor_relay_time, timezone)} · Worker stream: {timeLabel(model.worker_stream_time, timezone)} · Backend received: {timeLabel(model.last_ml_received_at, timezone)}</p>
          {(model.fits || []).map((fit) => <p key={fit.fit_id}>{fit.fit_purpose?.replaceAll("_", " ")}{fit.fold != null ? ` · CV ${fit.fold}` : ""} · {fit.seconds == null ? "Duration unknown" : `${Number(fit.seconds).toFixed(2)}s`}{fit.early_stopped ? ` · Early stopping (${fit.epochs_completed}/${fit.epochs})` : ""}</p>)}
        </div>)}
      </details>
      <div className="training-monitor-log-heading"><h3>Recent activity</h3>
        <label><input type="checkbox" checked={errorsOnly} onChange={(event) => setErrorsOnly(event.target.checked)} /> Errors only</label>
        <label><input type="checkbox" checked={autoScroll} onChange={(event) => setAutoScroll(event.target.checked)} /> Auto-scroll</label>
      </div>
      <div className="training-monitor-logs" ref={logContainer} role="log" aria-label="Recent training activity" tabIndex={0}>
        <table><thead><tr><th>Time</th><th>Model / group</th><th>What happened</th></tr></thead>
          <tbody>{events.length === 0 && <tr><td colSpan={3}>{errorsOnly ? "No errors in the received activity." : "Waiting for the first training update."}</td></tr>}
            {events.map((event) => <tr key={event.sequence} className={event.level === "error" ? "training-monitor-error-row" : ""}>
            <td title={event.details?.source_time ? `Emitted ${timeLabel(event.details.source_time, timezone)} · Received ${timeLabel(event.time, timezone)}` : "Received time"}>{timeLabel(event.time, timezone)}</td><td>{models.find((model) => model.id === event.model)?.name || branchNames[event.branch] || "All models"}</td>
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
  const [capabilities, setCapabilities] = useState(null);
  const [, setTick] = useState(0);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    api.trainingCapabilities({ signal: controller.signal }).then((value) => {
      if (!controller.signal.aborted) setCapabilities(value);
    }).catch(() => {});
    return () => controller.abort();
  }, [enabled]);

  useEffect(() => {
    if (!enabled || !items.length) return undefined;
    const timer = window.setInterval(() => { if (!document.hidden) setTick((value) => value + 1); }, 1000);
    return () => window.clearInterval(timer);
  }, [enabled, items.length]);

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
        setError(""); setReceivedAt({ time: new Date().toISOString(), clock: performance.now() });
      } catch (failure) {
        if (!disposed && !request.signal.aborted && failure.name !== "AbortError") setError("Training updates are unavailable. The last known state is shown.");
      } finally {
        if (!disposed && controller === request && !document.hidden) timer = window.setTimeout(refresh, 2000);
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
    {error && <div className="training-monitor-error" role="alert">{error}{receivedAt && ` Last received: ${timeLabel(receivedAt.time, region?.timezone)}.`}</div>}
    {items.map((run) => <RunCard key={run.training_run_id} run={run} timezone={region?.timezone}
      capabilities={capabilities}
      snapshotAgeSeconds={receivedAt ? Math.max(0, performance.now() - receivedAt.clock) / 1000 : 0}
      onDismiss={(id) => setItems((current) => current.filter((item) => item.training_run_id !== id))} />)}
  </div>;
}
