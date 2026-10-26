import React, { useEffect, useRef, useState } from "react";
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
  const [selected, setSelected] = useState(() => Math.max(0, report.points.length - 1));
  const chartContainer = useRef(null);
  const [width, setWidth] = useState(960);
  useEffect(() => {
    if (!chartContainer.current) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0) setWidth(Math.round(entry.contentRect.width));
    });
    observer.observe(chartContainer.current);
    return () => observer.disconnect();
  }, []);
  const points = report.points;
  const compact = width < 600;
  const multiDay = points.length > 24;
  const height = compact ? 280 : 360;
  const left = compact ? 60 : 84;
  const right = multiDay ? 32 : 24;
  const top = 24;
  const bottom = multiDay ? 64 : 52;
  const values = points.flatMap((point) => [point.reference, point.prediction]).filter((value) => value != null);
  const low = Math.min(0, ...values);
  const high = Math.max(1, ...values) * 1.08;
  const x = (index) => left + (points.length === 1 ? 0.5 : index / (points.length - 1)) * (width - left - right);
  const y = (value) => height - bottom - ((value - low) / (high - low)) * (height - top - bottom);
  const currentIndex = Math.min(selected, Math.max(0, points.length - 1));
  const current = points[currentIndex];
  const referenceLabel = "Actual traffic";

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
  const tickCount = Math.min(compact ? 3 : 6, points.length);
  const ticks = [...new Set(Array.from({ length: tickCount }, (_, index) => (
    Math.round(index * (points.length - 1) / Math.max(1, tickCount - 1))
  )))];

  return (
    <div className={`quality-chart${compact ? " quality-chart-compact" : ""}`}>
      <div className="quality-legend">
        <span><i className="quality-reference-key" />{referenceLabel}</span>
        <span><i className="quality-prediction-key" />Predicted traffic</span>
        <span className="quality-chart-unit">Vehicles/hour</span>
      </div>
      <div className="quality-chart-scroll" ref={chartContainer}>
        <svg viewBox={`0 0 ${width} ${height}`} role="img" tabIndex={0}
          aria-label="Actual and predicted traffic over the selected period. Use left and right arrow keys to inspect hours."
          onMouseMove={selectAt}
          onKeyDown={(event) => {
            if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
            event.preventDefault();
            setSelected(Math.max(0, Math.min(points.length - 1, currentIndex + (event.key === "ArrowRight" ? 1 : -1))));
          }}>
          <desc>Dataset traffic_volume compared with model predictions at the same hour. Vertical connectors show each difference.</desc>
          {[0, 1, 2, 3, 4].map((index) => {
            const value = low + index / 4 * (high - low);
            return <g key={index}>
              <line x1={left} x2={width - right} y1={y(value)} y2={y(value)} className="quality-grid-line" />
              <text x={left - 12} y={y(value) + 4} textAnchor="end">{Math.round(value).toLocaleString("en")}</text>
            </g>;
          })}
          {ticks.map((index) => <text key={index} x={x(index)} y={height - (multiDay ? 36 : 18)} textAnchor="middle">
            {multiDay && <tspan x={x(index)}>{new Intl.DateTimeFormat("en-GB", {
              timeZone: report.timezone, day: "2-digit", month: "short",
            }).format(new Date(points[index].forecast_for))}</tspan>}
            <tspan x={x(index)} dy={multiDay ? 18 : 0}>{timeText(points[index].forecast_for, report.timezone, true)}</tspan>
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
        <div className="quality-selected-hour"><span>Hour</span><strong>{timeText(current.forecast_for, report.timezone)}</strong></div>
        <div><span>Actual</span><strong>{numberText(current.reference)}</strong></div>
        <div><span>Predicted</span><strong>{numberText(current.prediction)}</strong></div>
        <div><span>Difference</span><strong>{numberText(current.absolute_error)}</strong></div>
      </div>}
    </div>
  );
}


export default function ModelQualityPanel({ region, enabled }) {
  const [report, setReport] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [hours, setHours] = useState(168);

  useEffect(() => {
    setReport(null);
    setError("");
    setLoading(false);
    if (!enabled || !region?.active_model_version_id) return;

    let active = true;
    let controller;
    let requestNumber = 0;
    let timer;
    async function load() {
      if (!active || document.visibilityState === "hidden") return;
      clearTimeout(timer);
      controller?.abort();
      controller = new AbortController();
      const request = ++requestNumber;
      setLoading(true);
      setError("");
      try {
        const result = await api.modelQuality(region.id, {
          modelVersionId: region.active_model_version_id, hours,
        }, { signal: controller.signal });
        if (!active || request !== requestNumber) return;
        if (result.window !== "recent" || result.mode !== "reference" || result.reference_source !== "dataset_traffic_volume"
            || result.selected_hours !== hours || !Number.isFinite(Date.parse(result.server_time)) || !Number.isFinite(Date.parse(result.next_refresh_at))) {
          throw new Error("Update the backend to support the selected time range and hourly refresh.");
        }
        setReport(result);
        // Use fresh server time, so a cached report or an incorrect device clock
        // does not shift the next refresh away from the region's next full hour.
        timer = setTimeout(load, Math.max(100, Date.parse(result.next_refresh_at) - Date.parse(result.server_time)));
      } catch (err) {
        if (active && request === requestNumber && err.name !== "AbortError") {
          setError(err.status === 404
            ? "Model Quality is unavailable on this backend. Update the backend before using this chart."
            : err.message);
          timer = setTimeout(load, 30_000);
        }
      } finally {
        if (active && request === requestNumber) setLoading(false);
      }
    }
    load();
    const onVisible = () => {
      if (document.visibilityState === "visible") {
        load();
      } else {
        clearTimeout(timer);
        controller?.abort();
        requestNumber += 1;
        setLoading(false);
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      active = false;
      controller?.abort();
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [enabled, region?.id, region?.active_model_version_id, refresh, hours]);

  const visibleReport = report?.region_id === region?.id
    && report?.active_model_version_id === region?.active_model_version_id && report?.selected_hours === hours ? report : null;

  return (
    <section className="card quality-panel">
      <div className="quality-heading">
        <div>
          <h2>Traffic comparison</h2>
          {visibleReport?.end_at && <p className="quality-period">
            {timeText(visibleReport.start_at, visibleReport.timezone)} — {timeText(visibleReport.end_at, visibleReport.timezone)}
          </p>}
        </div>
        <div className="quality-actions">
          <select aria-label="Time range" value={hours} onChange={(event) => setHours(Number(event.target.value))}>
            <option value={24}>Last 24 hours</option>
            <option value={168}>Last 7 days</option>
            <option value={336}>Last 14 days</option>
          </select>
          <button type="button" disabled={!enabled || loading || !region?.active_model_version_id} onClick={() => setRefresh((value) => value + 1)}>
            {loading ? "Updating…" : "Refresh"}
          </button>
        </div>
      </div>
      {!region?.active_model_version_id && <div className="alert alert-info">This region has no active model.</div>}
      {error && <div className="alert alert-error" role="alert">{error}</div>}
      {loading && !visibleReport && <p role="status">Loading traffic data…</p>}
      {visibleReport && <>
        <div className="quality-metrics">
          <div><span>Average difference</span><strong>{numberText(visibleReport.metrics.mae)}<span className="quality-metric-unit">vehicles/hour</span></strong></div>
          <div><span>Hours compared</span><strong>{visibleReport.coverage.paired_points} / {visibleReport.coverage.requested_points}</strong></div>
        </div>
        {visibleReport.coverage.dataset_points === 0
          ? <div className="alert alert-info">No traffic data available after training in this period.</div>
          : visibleReport.coverage.paired_points === 0 && <div className="alert alert-info">Not enough data to compare actual and predicted traffic.</div>}
        <QualityChart key={`${visibleReport.generated_at}-${visibleReport.mode}`} report={visibleReport} />
        <details className="quality-details"><summary>View details</summary>
          <dl className="quality-report-meta">
            <div><dt>Model</dt><dd>{visibleReport.model_version} · {visibleReport.model_variant}</dd></div>
            <div><dt>Dataset</dt><dd>{visibleReport.dataset_filename}</dd></div>
            <div><dt>Time zone</dt><dd>{visibleReport.timezone}</dd></div>
            <div><dt>RMSE</dt><dd>{numberText(visibleReport.metrics.rmse)} vehicles/hour</dd></div>
          </dl>
          <div className="table-wrap quality-hour-table"><table>
            <thead><tr><th>Hour</th><th>Actual</th><th>Predicted</th><th>Difference</th></tr></thead>
            <tbody>{visibleReport.points.map((point) => <tr key={point.forecast_for}>
              <td>{timeText(point.forecast_for, visibleReport.timezone)}</td><td>{numberText(point.reference)}</td>
              <td>{numberText(point.prediction)}</td>
              <td>{numberText(point.absolute_error)}</td>
            </tr>)}</tbody>
          </table></div>
        </details>
      </>}
    </section>
  );
}
