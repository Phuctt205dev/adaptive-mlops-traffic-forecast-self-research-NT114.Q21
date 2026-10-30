import React, { useEffect, useState } from "react";
import { api } from "./api";

function ContributionChart({ rows, label }) {
  const [showAll, setShowAll] = useState(false);
  const shown = showAll ? rows : rows.slice(0, 10);
  return <>
    <ol className="feature-chart" aria-label={label}>
      {shown.map((row, index) => <li key={row.label} className="feature-row">
        <div className="feature-label"><span className="feature-rank">{index + 1}</span><span>{row.label}</span></div>
        <div className="feature-bar-track" aria-hidden="true"><span style={{ width: `${row.share_percent}%` }} /></div>
        <strong className="feature-share">{row.share_percent.toLocaleString("en", { maximumFractionDigits: 1 })}%</strong>
      </li>)}
    </ol>
    {rows.length > 10 && <button type="button" className="feature-show-all" onClick={() => setShowAll(!showAll)}>
      {showAll ? "Show top 10" : `Show all ${rows.length} inputs`}</button>}
  </>;
}

export default function FeatureImportancePanel({ region, enabled }) {
  const [report, setReport] = useState(null);
  const [selectedModel, setSelectedModel] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => { setSelectedModel(""); }, [region?.id, region?.active_model_version_id]);
  useEffect(() => {
    if (!enabled || !region?.id) { setError(""); setLoading(false); return; }
    let active = true;
    let controller;
    let requestNumber = 0;
    async function load() {
      controller?.abort();
      const current = ++requestNumber;
      const requestController = new AbortController();
      controller = requestController;
      setLoading(true);
      setError("");
      try {
        const result = await api.featureImportance(region.id, region.active_model_version_id, { signal: requestController.signal });
        if (active && current === requestNumber && !requestController.signal.aborted) setReport(result);
      } catch (failure) {
        if (active && current === requestNumber && failure.name !== "AbortError") {
          setReport(null);
          setError(failure.status === 404 ? "Update the backend to view feature importance." : failure.message);
        }
      } finally {
        if (active && current === requestNumber) setLoading(false);
      }
    }
    const onVisible = () => {
      if (document.visibilityState === "visible") load();
      else { requestNumber += 1; controller?.abort(); setLoading(false); }
    };
    if (document.visibilityState !== "hidden") load();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      active = false;
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [enabled, region?.id, region?.active_model_version_id, refresh]);

  const visible = report?.region_id === region?.id
    && (report?.active_model_version_id ?? null) === (region?.active_model_version_id ?? null) ? report : null;
  const average = visible?.model_average;
  const models = !region?.active_model_version_id ? [] : visible?.model_reports?.length ? visible.model_reports
    : visible?.model_variant ? [{ ...visible, model_name: visible.model_variant, is_serving: true }] : [];
  const selected = models.find((model) => model.model_name === selectedModel)
    || models.find((model) => model.is_serving) || models[0];
  const rows = selected?.features || [];
  const leading = rows.find((row) => row.mean_abs_shap > 0);
  const chartKey = `${region?.id}:${region?.active_model_version_id}:${refresh}`;

  return <div className="feature-sections">
    <section className="card feature-panel" aria-labelledby="feature-average-title">
      <div className="quality-heading">
        <div><h2 id="feature-average-title">Average across five models</h2>
          <p className="feature-subtitle">Reports from the activated training run</p></div>
        <button type="button" disabled={loading || !region?.id}
          onClick={() => setRefresh((value) => value + 1)}>{loading ? "Updating…" : "Refresh"}</button>
      </div>
      {error && <div className="alert alert-error" role="alert">{error}</div>}
      {loading && !visible && <p role="status">Loading feature importance…</p>}
      {average && <p className="feature-coverage"><strong>{average.used_reports} of 5</strong> model reports included</p>}
      {!region?.active_model_version_id && <div className="alert alert-info">Activate a model on the Models page to choose a training run.</div>}
      {region?.active_model_version_id && average?.status === "incomplete" && <div className="alert alert-info">
        The five-model average needs a verified report with measurable contributions from every model. Train all five models to create missing reports. Available reports can be viewed below.
      </div>}
      {visible && !average && <div className="alert alert-info">Update the backend to view reports for all five models.</div>}
      {average?.status === "ready" && <>
        <ContributionChart key={`average:${chartKey}`} rows={average.features} label="Average input contributions across five models" />
        <details className="quality-details"><summary>View details</summary>
          <p>Random Forest, XGBoost, LightGBM, LSTM and GRU each have the same weight: 20%. Each report is converted to percentages before averaging. Related encoded inputs are grouped together.</p>
          <p>All reports belong to the training run selected by Activate on the Models page. Percentages show relative influence, not prediction accuracy or whether an input increases or decreases a prediction. This average does not choose features for retraining.</p>
          <div className="table-wrap"><table><thead><tr><th>Input</th><th>Average contribution</th><th>Present in reports</th></tr></thead>
            <tbody>{average.features.map((row) => <tr key={row.label}><td>{row.label}</td>
              <td>{row.share_percent.toLocaleString("en", { maximumFractionDigits: 1 })}%</td><td>{row.reports_present} of 5</td></tr>)}</tbody>
          </table></div>
        </details>
      </>}
      {models.length > 0 && <details className="quality-details"><summary>Report availability</summary>
        <div className="table-wrap"><table><thead><tr><th>Model</th><th>Report</th></tr></thead>
          <tbody>{models.map((model) => <tr key={model.model_name}><td>{model.model_name}</td>
            <td>{model.status === "ready" ? model.features.some((row) => row.mean_abs_shap > 0) ? "Ready" : "No measurable contribution" : model.message}</td></tr>)}</tbody>
        </table></div>
      </details>}
    </section>

    <section className="card feature-panel" aria-labelledby="feature-model-title">
      <div className="quality-heading"><div><h2 id="feature-model-title">Model details</h2>
        <p className="feature-subtitle">{visible?.model_version ? `${visible.model_version} · ${visible.dataset_filename || ""}` : "Choose a model to see its input contributions"}</p></div></div>
      {models.length > 0 && <div className="feature-model-picker"><label htmlFor="feature-model-choice">Model</label>
        <select id="feature-model-choice" value={selected?.model_name || ""} onChange={(event) => setSelectedModel(event.target.value)}>
          {models.map((model) => <option key={model.model_name} value={model.model_name}>
            {model.model_name}{model.is_serving ? " · Currently serving" : ""}
          </option>)}
        </select>
      </div>}
      {selected?.status !== "ready" && selected?.message && <div className="alert alert-info">{selected.message}</div>}
      {selected?.status === "ready" && <>
        {leading ? <div className="feature-highlight"><span>Most influential input for {selected.model_name}</span><strong>{leading.label}</strong></div>
          : <div className="alert alert-info">No measurable input contribution in this report.</div>}
        <ContributionChart key={`model:${selected.model_name}:${chartKey}`} rows={rows} label="Input contributions to selected model predictions" />
        <details className="quality-details"><summary>View details</summary>
          <p>Percentages show each input’s share of total SHAP influence, not prediction accuracy. Choosing a model here only changes this report.</p>
          {selected.sequence_length && <p>Influence is added across the previous {selected.sequence_length} hours in each input sequence, then averaged across sequences.</p>}
          <dl className="quality-report-meta">
            <div><dt>Model</dt><dd>{selected.model_name}</dd></div>
            <div><dt>Activated version</dt><dd>{visible.model_version}</dd></div>
            <div><dt>Dataset</dt><dd>{visible.dataset_filename}</dd></div>
            <div><dt>Records explained</dt><dd>{selected.sample_count}</dd></div>
            <div><dt>Report created</dt><dd>{new Date(selected.generated_at).toLocaleString()}</dd></div>
          </dl>
          <div className="table-wrap"><table><thead><tr><th>Input</th><th>Model features</th><th>Mean influence</th></tr></thead>
            <tbody>{rows.map((row) => <tr key={row.label}><td>{row.label}</td><td>{row.features.join(", ")}</td>
              <td>{row.mean_abs_shap.toLocaleString("en", { maximumFractionDigits: 2 })}</td></tr>)}</tbody>
          </table></div>
        </details>
      </>}
    </section>
  </div>;
}
