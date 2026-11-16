import React, { useId, useState } from "react";
import AppDialog from "./AppDialog.jsx";

export default function TrainingConfiguration({ value, onChange }) {
  const [advanced, setAdvanced] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const prefix = useId();
  const update = (key, next) => onChange({ ...value, [key]: next });

  function numberField(key, label, options = {}) {
    return <label htmlFor={`${prefix}-${key}`}>
      {label}
      <input id={`${prefix}-${key}`} type="number" required value={value[key]}
        min={options.min ?? 1} max={options.max} step={options.step ?? 1}
        onChange={(event) => update(key, event.target.value)} />
    </label>;
  }

  function fields() {
    return <div id={`${prefix}-fields`} key={advanced ? "advanced" : "basic"}
      className={`training-config-face${advanced ? " is-advanced" : ""}`}
      tabIndex={advanced ? 0 : undefined} role={advanced ? "region" : undefined}
      aria-label={advanced ? "Advanced training settings" : undefined}>
      <p className="training-config-intro">Choose the time window for your next training run.</p>
      <div className="training-config-dates">
        <label htmlFor={`${prefix}-start`}>Start date
          <input id={`${prefix}-start`} type="date" required value={value.train_start_date}
            max={value.train_end_date || undefined}
            onChange={(event) => update("train_start_date", event.target.value)} />
        </label>
        <label htmlFor={`${prefix}-end`}>End date
          <input id={`${prefix}-end`} type="date" required value={value.train_end_date}
            min={value.train_start_date || undefined}
            onChange={(event) => update("train_end_date", event.target.value)} />
        </label>
      </div>
      <p className="training-config-hint">The end date is excluded from training. Advanced settings also apply when hidden.</p>
      {advanced && <div className="training-config-advanced">
        <fieldset>
          <legend>All selected models</legend>
          <div className="form-grid">
            {numberField("cv_splits", "CV folds", { min: 2 })}
            {numberField("random_state", "Random seed", { min: 0, max: 4294967295 })}
            {numberField("final_test_ratio", "Final test ratio", { min: 0, max: 0.5, step: "any" })}
          </div>
          <p className="training-config-hint">For example, 0.15 reserves the latest 15% of data for final testing. Model selection uses CV results.</p>
        </fieldset>
        <fieldset>
          <legend>LSTM and GRU only</legend>
          <div className="form-grid">
            {numberField("recurrent_sequence_length", "Sequence length (hours)")}
            {numberField("recurrent_epochs", "Epoch limit per CV fold")}
            {numberField("recurrent_batch_size", "Batch size")}
          </div>
          <p className="training-config-hint">CV may stop early. Final training uses the epoch count selected from CV.</p>
        </fieldset>
        <fieldset className="training-config-managed">
          <legend>System managed · no training control</legend>
          <div className="form-grid">
            <div className="training-config-managed-field">
              <label htmlFor={`${prefix}-artifact-root`}>artifact_root
                <input id={`${prefix}-artifact-root`} value={value.artifact_root} readOnly aria-describedby={`${prefix}-artifact-note`} />
              </label>
              <small id={`${prefix}-artifact-note`}>The system manages where training results are saved.</small>
            </div>
            <div className="training-config-managed-field">
              <label htmlFor={`${prefix}-model-role`}>model_role
                <input id={`${prefix}-model-role`} value={value.model_role} readOnly aria-describedby={`${prefix}-role-note`} />
              </label>
              <small id={`${prefix}-role-note`}>New models remain candidates until you Activate them.</small>
            </div>
          </div>
        </fieldset>
      </div>}
    </div>;
  }

  return <>
    <section className="card training-config-card" aria-labelledby={`${prefix}-title`}>
      <div className="card-header">
        <h2 id={`${prefix}-title`}>Training Configuration</h2>
        <div className="training-config-actions">
          <div className="training-config-modes" role="group" aria-label="Training configuration mode">
            <button type="button" aria-pressed={!advanced} aria-controls={`${prefix}-fields`}
              onClick={() => setAdvanced(false)}>Basic</button>
            <button type="button" aria-pressed={advanced} aria-controls={`${prefix}-fields`}
              onClick={() => setAdvanced(true)}>Advanced</button>
          </div>
          {advanced && <button type="button" className="secondary training-config-expand" aria-haspopup="dialog"
            aria-label="Expand training configuration" title="Expand training configuration" onClick={() => setExpanded(true)}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
              <path d="M14 4h6v6M20 4l-7 7M10 20H4v-6M4 20l7-7" />
            </svg>
            Expand
          </button>}
        </div>
      </div>
      {!expanded ? fields() : <p className="muted">Advanced settings are open in the expanded view.</p>}
    </section>
    {expanded && <AppDialog title="Advanced training configuration" size="wide"
      description="Changes are kept in your current training setup when you close this window."
      onClose={() => setExpanded(false)}
      footer={<button type="button" onClick={() => setExpanded(false)}>Done</button>}>
      <div className="training-config-expanded">{fields()}</div>
    </AppDialog>}
  </>;
}
