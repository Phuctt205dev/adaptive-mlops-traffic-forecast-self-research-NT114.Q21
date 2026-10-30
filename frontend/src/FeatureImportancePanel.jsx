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

const reportStatuses = {
  included: "Included", missing: "No report yet", unavailable: "Report unavailable",
  unsupported: "Unsupported model", invalid: "Report could not be verified", zero: "No measurable contribution",
};

export default function FeatureImportancePanel({ region, enabled }) {
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);

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
  const history = visible?.history;
  const rows = visible?.features || [];
  const leading = rows.find((row) => row.mean_abs_shap > 0);
  const chartKey = `${region?.id}:${region?.active_model_version_id}:${refresh}`;

  return <div className="feature-sections">
    <section className="card feature-panel" aria-labelledby="feature-history-title">
      <div className="quality-heading">
        <div><h2 id="feature-history-title">Across saved models</h2>
          <p className="feature-subtitle">Average input contribution in this region</p></div>
        <button type="button" disabled={loading || !region?.id}
          onClick={() => setRefresh((value) => value + 1)}>{loading ? "Updating…" : "Refresh"}</button>
      </div>
      {error && <div className="alert alert-error" role="alert">{error}</div>}
      {loading && !visible && <p role="status">Loading feature importance…</p>}
      {history && <p className="feature-coverage"><strong>{history.used_reports} of {history.total_models}</strong> saved models included</p>}
      {history?.status === "pending" && <div className="alert alert-info">
        {history.total_models ? "No saved report with measurable contributions is available yet." : "This region has no saved models yet."}
      </div>}
      {visible && !history && <div className="alert alert-info">Update the backend to view the average across saved models.</div>}
      {history?.status === "ready" && <>
        <ContributionChart key={`history:${chartKey}`} rows={history.features} label="Average input contributions across saved models" />
        <details className="quality-details"><summary>View details</summary>
          <p>Each included model has the same weight. Percentages are calculated within each report before averaging; they do not measure prediction accuracy or whether an input increases or decreases a prediction.</p>
          <p>This is a history of saved versions, not an equal vote from each model type. It includes {history.dataset_count} dataset{history.dataset_count === 1 ? "" : "s"}; different datasets and training periods can affect the average.</p>
          <p>Missing, unavailable, invalid and all-zero reports are excluded. An input absent from an included report adds no share to this average; this does not prove that input is unimportant to every model.</p>
          <div className="table-wrap"><table><thead><tr><th>Input</th><th>Average contribution</th><th>Present in reports</th></tr></thead>
            <tbody>{history.features.map((row) => <tr key={row.label}><td>{row.label}</td>
              <td>{row.share_percent.toLocaleString("en", { maximumFractionDigits: 1 })}%</td><td>{row.reports_present} of {history.used_reports}</td></tr>)}</tbody>
          </table></div>
        </details>
      </>}
      {history?.models.length > 0 && <details className="quality-details"><summary>Reports included and excluded</summary>
        <div className="table-wrap"><table><thead><tr><th>Saved version</th><th>Model</th><th>Dataset</th><th>Report</th></tr></thead>
          <tbody>{history.models.map((model) => <tr key={model.model_version_id}><td>{model.model_version}</td><td>{model.model_variant}</td>
            <td>{model.dataset_filename || "Unavailable"}</td><td>{reportStatuses[model.status]}</td></tr>)}</tbody>
        </table></div>
      </details>}
    </section>

    <section className="card feature-panel" aria-labelledby="feature-active-title">
      <div className="quality-heading"><div><h2 id="feature-active-title">Active model</h2>
        <p className="feature-subtitle">{visible?.model_version ? `${visible.model_version} · ${visible.model_variant}` : "The model activated on the Models page"}</p></div></div>
      {!region?.active_model_version_id && <div className="alert alert-info">This region has no active model.</div>}
      {visible?.status !== "ready" && region?.active_model_version_id && visible?.message && <div className="alert alert-info">{visible.message}</div>}
      {visible?.status === "ready" && <>
        {leading ? <div className="feature-highlight"><span>Most influential input</span><strong>{leading.label}</strong></div>
          : <div className="alert alert-info">No measurable input contribution in this report.</div>}
        <ContributionChart key={`active:${chartKey}`} rows={rows} label="Input contributions to active model predictions" />
        <details className="quality-details"><summary>View details</summary>
          <p>Percentages show each input’s share of total SHAP influence, not prediction accuracy. Related encoded features are grouped together.</p>
          {visible.sequence_length && <p>For this model, influence is added across the previous {visible.sequence_length} hours in each input sequence. Gradient-based estimates explain this model’s predictions.</p>}
          <dl className="quality-report-meta">
            <div><dt>Model</dt><dd>{visible.model_version} · {visible.model_variant}</dd></div>
            <div><dt>Dataset</dt><dd>{visible.dataset_filename}</dd></div>
            <div><dt>Records explained</dt><dd>{visible.sample_count}</dd></div>
            {visible.sequence_length && <div><dt>Past hours per record</dt><dd>{visible.sequence_length}</dd></div>}
            <div><dt>Report created</dt><dd>{new Date(visible.generated_at).toLocaleString()}</dd></div>
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
