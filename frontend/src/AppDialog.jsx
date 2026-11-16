import React, { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";

export default function AppDialog({ title, description, children, footer, onClose, busy = false,
  size = "medium", tone = "default", initialFocusRef, role = "dialog" }) {
  const dialogRef = useRef(null);
  const headingRef = useRef(null);
  const backdropPress = useRef(false);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const dialog = dialogRef.current;
    const previousFocus = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialog.showModal();
    (initialFocusRef?.current || headingRef.current)?.focus();
    return () => {
      dialog.close();
      document.body.style.overflow = previousOverflow;
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, []);

  function outside(event) {
    const box = dialogRef.current.getBoundingClientRect();
    return event.target === dialogRef.current &&
      (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom);
  }

  function keepFocus(event) {
    if (event.key !== "Tab") return;
    const controls = [...dialogRef.current.querySelectorAll(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])',
    )].filter((element) => element.getClientRects().length && element.tabIndex >= 0);
    const first = controls[0];
    const last = controls[controls.length - 1];
    const active = document.activeElement;
    if (!first) {
      event.preventDefault();
      headingRef.current.focus();
    } else if (event.shiftKey && (active === first || !controls.includes(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !controls.includes(active))) {
      event.preventDefault();
      first.focus();
    }
  }

  return createPortal(
    <dialog ref={dialogRef} className={`app-dialog app-dialog-${size} app-dialog-${tone}`}
      role={role} aria-modal="true" aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined} aria-busy={busy}
      onKeyDown={keepFocus}
      onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}
      onPointerDown={(event) => { backdropPress.current = outside(event); }}
      onClick={(event) => { if (!busy && backdropPress.current && outside(event)) onClose(); backdropPress.current = false; }}>
      <header className="app-dialog-header">
        <div>
          {tone === "danger" && <span className="app-dialog-eyebrow">Permanent action</span>}
          <h2 ref={headingRef} id={titleId} tabIndex={-1}>{title}</h2>
          {description && <p id={descriptionId}>{description}</p>}
        </div>
        <button type="button" className="app-dialog-close" aria-label="Close dialog" disabled={busy} onClick={onClose}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
            <path d="m6 6 12 12M18 6 6 18" />
          </svg>
        </button>
      </header>
      <div className="app-dialog-body">{children}</div>
      {footer && <footer className="app-dialog-footer">{footer}</footer>}
    </dialog>, document.body,
  );
}
