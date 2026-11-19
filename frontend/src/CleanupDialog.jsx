import React, { useEffect, useRef, useState } from "react";
import AppDialog from "./AppDialog.jsx";
import { api } from "./api.js";
import { cleanupPhases, cleanupView, pendingCleanupKey, readPendingCleanup } from "./cleanupProgress.js";

// DELETE runs once per explicit confirmation. Every automatic request is GET.
// Aborting a browser request does not imply that server cleanup has stopped.
export default function CleanupDialog({ kind, id, subject, description, userId, onDeleted, onClose, onBusyChange }) {
  const saved = readPendingCleanup(userId, kind);
  const [mode, setMode] = useState(saved?.id === id ? "checking" : "confirm");
  const [status, setStatus] = useState(null);
  const [error, setError] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const mounted = useRef(true);
  const writing = useRef(false);
  const completed = useRef(false);
  const closeRef = useRef(null);
  const callbacks = useRef({ onDeleted, onClose, onBusyChange });
  callbacks.current = { onDeleted, onClose, onBusyChange };
  const storageKey = pendingCleanupKey(userId, kind);
  const busy = mode === "checking";

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; callbacks.current.onBusyChange?.(false); };
  }, []);

  function remember() {
    try { sessionStorage.setItem(storageKey, JSON.stringify({ id, subject })); } catch { /* unavailable storage */ }
  }

  async function finish() {
    if (!mounted.current || completed.current) return;
    completed.current = true;
    try { sessionStorage.removeItem(storageKey); } catch { /* unavailable storage */ }
    setMode("completed");
    try {
      await callbacks.current.onDeleted();
      if (mounted.current) callbacks.current.onClose();
    } catch {
      if (mounted.current) setError("Deletion is complete. Refresh the list to see the latest data.");
    }
  }

  useEffect(() => {
    callbacks.current.onBusyChange?.(busy);
    if (!busy) return;
    let stopped = false;
    let timer;
    let active;
    const started = Date.now();
    async function poll() {
      active = new AbortController();
      const timeout = setTimeout(() => active.abort(), 8000);
      try {
        const result = await api.cleanupStatus(kind, id, { signal: active.signal });
        if (stopped) return;
        setStatus(result);
        if (cleanupView(result) === "completed") { await finish(); return; }
        if (cleanupView(result) === "retry") {
          setError("Deletion paused. Its progress is saved. Retry to continue the unfinished steps.");
          setMode("retry"); return;
        }
        if (result.can_retry && !writing.current) {
          setMode("retry");
          setError("No deletion is currently in progress. Retry to continue; completed steps are kept.");
          return;
        }
        setError("");
      } catch {
        if (stopped) return;
        setError("Waiting for a response. The server may still be deleting files. No extra delete request has been sent.");
      } finally { clearTimeout(timeout); }
      const seconds = Math.floor((Date.now() - started) / 1000);
      setElapsed(seconds);
      if (seconds >= 180) {
        setMode("unknown");
        setError("The result is not yet confirmed. You can close this window and check again. Closing does not cancel deletion.");
      } else if (!stopped) timer = setTimeout(poll, 2000);
    }
    poll();
    return () => { stopped = true; clearTimeout(timer); active?.abort(); };
  }, [busy, id, kind]);

  async function remove() {
    if (writing.current) return;
    writing.current = true;
    remember();
    setError(""); setElapsed(0); setMode("checking");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 120000);
    try {
      await api.deleteCleanupItem(kind, id, { signal: controller.signal });
      await finish();
    } catch (failure) {
      if (!mounted.current || completed.current) return;
      // Reconcile a lost response, including gateways returning an error after
      // deletion committed. Only a confirmed preflight rejection permits reset.
      try {
        const result = await api.cleanupStatus(kind, id, { signal: AbortSignal.timeout(8000) });
        if (!mounted.current || completed.current) return;
        setStatus(result);
        if (result.state === "completed") { await finish(); return; }
        if (result.state === "retry_required" || result.can_retry) { setMode("retry"); setError(failure.message); }
        else if (result.state === "not_started" && failure.status && failure.status < 500) {
          try { sessionStorage.removeItem(storageKey); } catch { /* unavailable storage */ }
          setMode("confirm"); setError(failure.message);
        } else {
          setMode("unknown");
          setError("The response was interrupted. Check status before trying again; deletion may still be running.");
        }
      } catch {
        if (mounted.current) {
          setMode("unknown");
          setError("Could not confirm deletion. Check status when the connection returns. Do not assume it failed.");
        }
      }
    } finally { clearTimeout(timeout); writing.current = false; }
  }

  return <AppDialog title={`Delete ${kind}?`} description={description} tone="danger"
    role="alertdialog" busy={busy} onClose={onClose} initialFocusRef={closeRef}
    footer={<>
      <button ref={closeRef} type="button" className="secondary app-dialog-cancel" disabled={busy} onClick={onClose}>{mode === "confirm" ? "Cancel" : "Close"}</button>
      {mode === "unknown" && <button type="button" onClick={() => { setElapsed(0); setMode("checking"); }}>Check status</button>}
      {(mode === "confirm" || mode === "retry") && <button type="button" className="app-dialog-danger-button" onClick={remove}>
        {mode === "retry" ? "Retry deletion" : `Delete ${kind}`}
      </button>}
      {busy && <button type="button" className="app-dialog-danger-button" disabled>Deleting…</button>}
    </>}>
    <div className="app-dialog-subject"><strong>{subject}</strong></div>
    <p className="app-dialog-warning">This action cannot be undone.</p>
    {mode !== "confirm" && <div className="cleanup-progress" role="status" aria-live="polite">
      <strong>{cleanupPhases[status?.phase] || cleanupPhases.preparing}</strong>
      <p>{status?.done?.length ? `${status.done.length} cleanup steps completed. ` : ""}{busy ? `${elapsed}s elapsed.` : ""}</p>
      <p className="muted">Storage space may take a few minutes to update after files are deleted.</p>
    </div>}
    {error && <div className="alert alert-error" role="alert">{error}</div>}
  </AppDialog>;
}
