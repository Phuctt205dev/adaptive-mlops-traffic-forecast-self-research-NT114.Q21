import React, { useRef, useState } from "react";
import AppDialog from "./AppDialog.jsx";

export default function ConfirmDialog({ title, subject, description, confirmLabel, onConfirm, onClose }) {
  const cancelRef = useRef(null);
  const submittingRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function confirm() {
    if (submittingRef.current) return;
    submittingRef.current = true;
    setBusy(true);
    setError("");
    try {
      await onConfirm();
      onClose();
    } catch (err) {
      setError(err.message || "The action could not be completed. Please try again.");
    } finally {
      submittingRef.current = false;
      setBusy(false);
    }
  }

  return <AppDialog title={title} description={description} tone="danger" role="alertdialog"
    onClose={onClose} busy={busy} initialFocusRef={cancelRef}
    footer={<>
      <button ref={cancelRef} type="button" className="secondary app-dialog-cancel" disabled={busy} onClick={onClose}>Cancel</button>
      <button type="button" className="app-dialog-danger-button" disabled={busy} onClick={confirm}>
        {busy ? "Deleting…" : confirmLabel}
      </button>
    </>}>
    <div className="app-dialog-subject">
      <span className="app-dialog-subject-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
          <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7M14 10v7" />
        </svg>
      </span>
      <strong>{subject}</strong>
    </div>
    <p className="app-dialog-warning">This action cannot be undone.</p>
    {error && <div className="alert alert-error" role="alert">{error}</div>}
    {busy && <p className="muted" role="status">Deleting the selected item. Please keep this window open.</p>}
  </AppDialog>;
}
