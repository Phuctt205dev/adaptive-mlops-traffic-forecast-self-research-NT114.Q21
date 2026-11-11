import React, { useState } from "react";

export default function TrainingConfiguration({ value, onChange }) {
  const [advanced, setAdvanced] = useState(false);
  const update = (key, next) => onChange({ ...value, [key]: next });

  function numberField(key, label, options = {}) {
    return <label htmlFor={`training-${key}`}>
      {label}
      <input id={`training-${key}`} type="number" required value={value[key]}
        min={options.min ?? 1} max={options.max} step={options.step ?? 1}
        onChange={(event) => update(key, event.target.value)} />
    </label>;
  }

  return <section className="card training-config-card" aria-labelledby="training-config-title">
    <div className="card-header">
      <h2 id="training-config-title">Training Configuration</h2>
      <div className="training-config-modes" role="group" aria-label="Training configuration mode">
        <button type="button" aria-pressed={!advanced} aria-controls="training-config-fields"
          onClick={() => setAdvanced(false)}>Basic</button>
        <button type="button" aria-pressed={advanced} aria-controls="training-config-fields"
          onClick={() => setAdvanced(true)}>Advanced</button>
      </div>
    </div>
    <div id="training-config-fields" key={advanced ? "advanced" : "basic"}
      className={`training-config-face${advanced ? " is-advanced" : ""}`}
      tabIndex={advanced ? 0 : undefined} role={advanced ? "region" : undefined}
      aria-label={advanced ? "Advanced training settings" : undefined}>
      <p className="training-config-intro">Choose the time window for your next training run.</p>
      <div className="training-config-dates">
        <label htmlFor="training-start-date">Start date
          <input id="training-start-date" type="date" required value={value.train_start_date}
            max={value.train_end_date || undefined}
            onChange={(event) => update("train_start_date", event.target.value)} />
        </label>
        <label htmlFor="training-end-date">End date
          <input id="training-end-date" type="date" required value={value.train_end_date}
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
              <label htmlFor="training-artifact-root">artifact_root
                <input id="training-artifact-root" value={value.artifact_root} readOnly aria-describedby="training-artifact-note" />
              </label>
              <small id="training-artifact-note">The system manages where training results are saved.</small>
            </div>
            <div className="training-config-managed-field">
              <label htmlFor="training-model-role">model_role
                <input id="training-model-role" value={value.model_role} readOnly aria-describedby="training-role-note" />
              </label>
              <small id="training-role-note">New models remain candidates until you Activate them.</small>
            </div>
          </div>
        </fieldset>
      </div>}
    </div>
  </section>;
}
