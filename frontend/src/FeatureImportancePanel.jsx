import React, { useEffect, useState } from "react";
import { api } from "./api";


export default function FeatureImportancePanel({ region, enabled }) {
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    if (!enabled || !region?.active_model_version_id) { setError(""); setLoading(false); return; }
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
    setShowAll(false);
    if (document.visibilityState !== "hidden") load();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      active = false;
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [enabled, region?.id, region?.active_model_version_id, refresh]);

  const visible = report?.region_id === region?.id && report?.active_model_version_id === region?.active_model_version_id ? report : null;
  const rows = visible?.features || [];
  const shown = showAll ? rows : rows.slice(0, 10);
  const leading = rows.find((row) => row.mean_abs_shap > 0);

  return <section className="card feature-panel">
    <div className="quality-heading">
      <div><h2>Feature importance</h2><p className="feature-subtitle">What influences this model?</p></div>
      <button type="button" disabled={loading || !region?.active_model_version_id}
        onClick={() => setRefresh((value) => value + 1)}>{loading ? "Updating…" : "Refresh"}</button>
    </div>
    {!region?.active_model_version_id && <div className="alert alert-info">This region has no active model.</div>}
    {error && <div className="alert alert-error" role="alert">{error}</div>}
    {loading && !visible && <p role="status">Loading feature importance…</p>}
    {visible?.status !== "ready" && visible?.message && <div className="alert alert-info">{visible.message}</div>}
    {visible?.status === "ready" && <>
      {leading ? <div className="feature-highlight"><span>Most influential input</span><strong>{leading.label}</strong></div>
        : <div className="alert alert-info">No measurable input contribution in this report.</div>}
      <ol className="feature-chart" aria-label="Input contributions to model predictions">
        {shown.map((row, index) => <li key={row.label} className="feature-row">
          <div className="feature-label"><span className="feature-rank">{index + 1}</span><span>{row.label}</span></div>
          <div className="feature-bar-track" aria-hidden="true"><span style={{ width: `${row.share_percent}%` }} /></div>
          <strong className="feature-share">{row.share_percent.toLocaleString("en", { maximumFractionDigits: 1 })}%</strong>
        </li>)}
      </ol>
      {rows.length > 10 && <button type="button" className="feature-show-all" onClick={() => setShowAll(!showAll)}>
        {showAll ? "Show top 10" : `Show all ${rows.length} inputs`}</button>}
      <details className="quality-details"><summary>View details</summary>
        <p>Percentages show each input’s share of total SHAP influence, not prediction accuracy. Related encoded features are grouped together.</p>
        <dl className="quality-report-meta">
          <div><dt>Model</dt><dd>{visible.model_version} · {visible.model_variant}</dd></div>
          <div><dt>Dataset</dt><dd>{visible.dataset_filename}</dd></div>
          <div><dt>Records explained</dt><dd>{visible.sample_count}</dd></div>
          <div><dt>Report created</dt><dd>{new Date(visible.generated_at).toLocaleString()}</dd></div>
        </dl>
        <div className="table-wrap"><table><thead><tr><th>Input</th><th>Model features</th><th>Mean influence</th></tr></thead>
          <tbody>{rows.map((row) => <tr key={row.label}><td>{row.label}</td><td>{row.features.join(", ")}</td>
            <td>{row.mean_abs_shap.toLocaleString("en", { maximumFractionDigits: 2 })}</td></tr>)}</tbody>
        </table></div>
      </details>
    </>}
  </section>;
}
