import React, { useEffect, useState } from "react";
import { api } from "./api";


const numberText = (value) => value == null ? "—" : Number(value).toLocaleString("en", { maximumFractionDigits: 2 });

function timeText(value, timezone, short = false) {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone,
    ...(short ? {} : { day: "2-digit", month: "short", year: "numeric" }),
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format(new Date(value));
}


export function QualityChart({ report }) {
  const [selected, setSelected] = useState(0);
  const points = report.points;
  const width = 960;
  const height = 320;
  const left = 72;
  const right = 24;
  const top = 24;
  const bottom = 48;
  const values = points.flatMap((point) => [point.reference, point.prediction]).filter((value) => value != null);
  const low = Math.min(0, ...values);
  const high = Math.max(1, ...values) * 1.08;
  const x = (index) => left + (points.length === 1 ? 0.5 : index / (points.length - 1)) * (width - left - right);
  const y = (value) => height - bottom - ((value - low) / (high - low)) * (height - top - bottom);
  const currentIndex = Math.min(selected, Math.max(0, points.length - 1));
  const current = points[currentIndex];
  const referenceLabel = "Actual traffic (dataset)";

  function pathFor(key) {
    let previousExists = false;
    return points.map((point, index) => {
      if (point[key] == null) {
        previousExists = false;
        return "";
      }
      const command = previousExists ? "L" : "M";
      previousExists = true;
      return `${command}${x(index)},${y(point[key])}`;
    }).join(" ");
  }

  function selectAt(event) {
    const bounds = event.currentTarget.getBoundingClientRect();
    const position = (event.clientX - bounds.left) / bounds.width * width;
    const ratio = (position - left) / (width - left - right);
    setSelected(Math.max(0, Math.min(points.length - 1, Math.round(ratio * (points.length - 1)))));
  }

  if (!points.length) return <p className="muted">No chart values are available in this window.</p>;
  const ticks = [...new Set(Array.from({ length: Math.min(6, points.length) }, (_, index) => (
    Math.round(index * (points.length - 1) / Math.max(1, Math.min(6, points.length) - 1))
  )))];

  return (
    <div className="quality-chart">
      <div className="quality-legend">
        <span><i className="quality-reference-key" />{referenceLabel}</span>
        <span><i className="quality-prediction-key" />Model prediction</span>
        <small>vehicles/hour · {report.timezone}</small>
      </div>
      <div className="quality-chart-scroll">
        <svg viewBox={`0 0 ${width} ${height}`} role="img" tabIndex={0}
          aria-label={`${referenceLabel} and model prediction over time. Use left and right arrow keys to inspect hours.`}
          onMouseMove={selectAt}
          onKeyDown={(event) => {
            if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
            event.preventDefault();
            setSelected(Math.max(0, Math.min(points.length - 1, currentIndex + (event.key === "ArrowRight" ? 1 : -1))));
          }}>
          <desc>Gaps represent unavailable values. Errors use only observed traffic and prediction pairs in the last 24 completed hours.</desc>
          {[0, 1, 2, 3, 4].map((index) => {
            const value = low + index / 4 * (high - low);
            return <g key={index}>
              <line x1={left} x2={width - right} y1={y(value)} y2={y(value)} className="quality-grid-line" />
              <text x={left - 12} y={y(value) + 4} textAnchor="end">{Math.round(value).toLocaleString("en")}</text>
            </g>;
          })}
          {ticks.map((index) => <text key={index} x={x(index)} y={height - 18} textAnchor="middle">
            {timeText(points[index].forecast_for, report.timezone, true)}
          </text>)}
          {points.map((point, index) => point.absolute_error != null && <line key={`error-${point.forecast_for}`}
            x1={x(index)} x2={x(index)} y1={y(point.reference)} y2={y(point.prediction)} className="quality-error-line" />)}
          <path d={pathFor("reference")} className="quality-reference-line" />
          <path d={pathFor("prediction")} className="quality-prediction-line" />
          {points.map((point, index) => <g key={point.forecast_for}>
            {point.reference != null && <circle cx={x(index)} cy={y(point.reference)} r="2.5" className="quality-reference-dot" />}
            {point.prediction != null && <circle cx={x(index)} cy={y(point.prediction)} r="2.5" className="quality-prediction-dot" />}
          </g>)}
          <line x1={x(currentIndex)} x2={x(currentIndex)} y1={top} y2={height - bottom} className="quality-cursor" />
        </svg>
      </div>
      {current && <div className="quality-point-detail" aria-live="polite">
        <strong>{timeText(current.forecast_for, report.timezone)}</strong>
        <span>{referenceLabel}: <b>{numberText(current.reference)}</b></span>
        <span>Prediction: <b>{numberText(current.prediction)}</b></span>
        <span>Difference (prediction − actual): <b>{numberText(current.reference != null && current.prediction != null ? current.prediction - current.reference : null)}</b></span>
        <span>Absolute error: <b>{numberText(current.absolute_error)}</b></span>
        <span>Label source: <b>{current.label_source}</b></span>
      </div>}
      <p className="muted quality-chart-hint">Hover over the chart or focus it and use ← / →. Missing values remain gaps.</p>
    </div>
  );
}


export default function ModelQualityPanel({ region, enabled }) {
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    setReport(null);
    setError("");
    setLoading(false);
    if (!enabled || !region?.active_model_version_id) return;

    let active = true;
    let controller;
    let requestNumber = 0;
    async function load() {
      if (document.visibilityState === "hidden") return;
      controller?.abort();
      controller = new AbortController();
      const request = ++requestNumber;
      setLoading(true);
      setError("");
      try {
        const result = await api.modelQuality(region.id, {
          modelVersionId: region.active_model_version_id,
        }, { signal: controller.signal });
        if (!active || request !== requestNumber) return;
        if (result.window !== "recent" || result.mode !== "observed") {
          throw new Error("Update the backend to support automatic quality monitoring for the last 24 hours.");
        }
        setReport(result);
      } catch (err) {
        if (active && request === requestNumber && err.name !== "AbortError") {
          setError(err.status === 404
            ? "Model Quality is unavailable on this backend. Update the backend before using this chart."
            : err.message);
        }
      } finally {
        if (active && request === requestNumber) setLoading(false);
      }
    }
    load();
    const timer = setInterval(load, 60_000);
    const onVisible = () => { if (document.visibilityState === "visible") load(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      active = false;
      controller?.abort();
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [enabled, region?.id, region?.active_model_version_id, refresh]);

  const visibleReport = report?.region_id === region?.id
    && report?.active_model_version_id === region?.active_model_version_id ? report : null;

  return (
    <section className="card quality-panel">
      <div className="card-header">
        <h2>Model Quality</h2>
        <span className="quality-replay-badge">Last 24 completed hours</span>
      </div>
      <div className="quality-status">
        <p className="muted">The active model is compared with actual traffic recorded in the dataset. Updates automatically every minute while this tab is visible.</p>
        <button type="button" disabled={!enabled || loading || !region?.active_model_version_id} onClick={() => setRefresh((value) => value + 1)}>
          {loading ? "Updating…" : "Refresh"}
        </button>
      </div>
      {!region?.active_model_version_id && <div className="alert alert-info">This region has no active model.</div>}
      {error && <div className="alert alert-error" role="alert">{error}</div>}
      {loading && !visibleReport && <p role="status">Loading the last 24 completed hours…</p>}
      {visibleReport && <>
        <div className="quality-report-meta">
          <strong>{visibleReport.model_version} · {visibleReport.model_variant}</strong>
          <span>{visibleReport.dataset_filename}</span>
          <span>{timeText(visibleReport.start_at, visibleReport.timezone)} — before {timeText(visibleReport.window_end_at, visibleReport.timezone)}</span>
          <span>{visibleReport.timezone}</span>
        </div>
        <div className="quality-metrics">
          <div><span>MAE</span><strong>{numberText(visibleReport.metrics.mae)}</strong><small>vehicles/hour · average absolute error</small></div>
          <div><span>RMSE</span><strong>{numberText(visibleReport.metrics.rmse)}</strong><small>vehicles/hour · sensitive to large errors</small></div>
          <div><span>Evaluated pairs</span><strong>{visibleReport.coverage.paired_points} / {visibleReport.coverage.requested_points}</strong><small>eligible reference + prediction</small></div>
        </div>
        <p className="muted quality-coverage">
          Labels: {visibleReport.coverage.observed_labels} observed · {visibleReport.coverage.synthetic_labels} synthetic · {visibleReport.coverage.unknown_labels} unknown.
          {" "}{visibleReport.coverage.requested_points - visibleReport.coverage.dataset_points} missing dataset hours.
        </p>
        {visibleReport.coverage.dataset_points === 0
          ? <div className="alert alert-info">No eligible dataset rows in the last 24 hours. The chart keeps the current time window; it does not use older or future data.</div>
          : visibleReport.coverage.observed_labels === 0
            ? <div className="alert alert-info">No actual traffic is identified in this window. Synthetic and unverified dataset values are excluded, so accuracy cannot be calculated.</div>
            : visibleReport.coverage.paired_points === 0 && <div className="alert alert-info">No valid actual traffic and prediction pairs are available. Accuracy cannot be calculated yet.</div>}
        <p className="muted quality-chart-hint">Only dataset rows identified as observed traffic are compared. Generated and interpolated values are excluded.</p>
        <p className="muted quality-chart-hint">Predictions are recalculated with the current model and historical inputs; they are not a record of forecasts issued yesterday.</p>
        <QualityChart key={`${visibleReport.generated_at}-${visibleReport.mode}`} report={visibleReport} />
        <details className="quality-details"><summary>View hourly values and evaluation notes</summary>
          <ul>{visibleReport.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
          <div className="table-wrap quality-hour-table"><table>
            <thead><tr><th>Hour · {visibleReport.timezone}</th><th>Actual traffic</th><th>Prediction</th><th>Absolute error</th><th>Label source</th></tr></thead>
            <tbody>{visibleReport.points.map((point) => <tr key={point.forecast_for}>
              <td>{timeText(point.forecast_for, visibleReport.timezone)}</td><td>{numberText(point.reference)}</td>
              <td>{numberText(point.prediction)}</td><td>{numberText(point.absolute_error)}</td><td>{point.label_source}</td>
            </tr>)}</tbody>
          </table></div>
          <p className="muted">Report generated: {timeText(visibleReport.generated_at, visibleReport.timezone)}.</p>
        </details>
      </>}
    </section>
  );
}
