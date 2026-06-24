import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { api, getStoredToken, setStoredToken } from "./api";
import "./styles.css";

const pages = [
  ["dashboard", "Dashboard"],
  ["predict", "Prediction"],
  ["regions", "Regions"],
  ["datasets", "Datasets"],
  ["models", "Models"],
  ["drift", "Drift"],
  ["users", "Users"],
];

const trainingModelOptions = [
  {
    id: "random_forest_lag",
    label: "Random Forest",
    group: "Tree models",
  },
  {
    id: "xgboost_lag",
    label: "XGBoost",
    group: "Tree models",
  },
  {
    id: "lightgbm_lag",
    label: "LightGBM",
    group: "Tree models",
  },
  {
    id: "lstm",
    label: "LSTM",
    group: "Deep learning",
  },
  {
    id: "gru",
    label: "GRU",
    group: "Deep learning",
  },
];

const defaultTrainingModels = trainingModelOptions.map((option) => option.id);

function formatDate(value) {
  return value ? new Date(value).toLocaleString() : "-";
}

function isoToDatetimeLocalValue(value) {
  if (!value) return "";
  return value.slice(0, 16);
}

function localDatetimeToIsoWithOffset(value) {
  const date = new Date(value);
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const absOffset = Math.abs(offsetMinutes);
  const pad = (number) => String(number).padStart(2, "0");
  const normalizedValue = value.length === 16 ? `${value}:00` : value;
  return `${normalizedValue}${sign}${pad(Math.floor(absOffset / 60))}:${pad(absOffset % 60)}`;
}

function addDaysToIso(value, days) {
  const date = new Date(value);
  date.setDate(date.getDate() + days);
  return date;
}

function clampDate(date, minValue, maxValue) {
  const min = new Date(minValue);
  const max = new Date(maxValue);
  if (date < min) return min;
  if (date > max) return max;
  return date;
}

function toDatetimeLocalFromDate(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function toDateOnly(value) {
  if (!value) return "";
  const date = new Date(value);
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function currentForecastStart(window) {
  if (!window?.production_start_at || !window?.production_end_at) return null;
  const now = new Date();
  now.setMinutes(0, 0, 0);
  return clampDate(now, window.production_start_at, window.production_end_at);
}

function trainingWindowForDateRange(startAt, endAt) {
  if (!startAt || !endAt) return null;
  const start = new Date(startAt);
  const end = new Date(endAt);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const trainEndLimit = new Date(today.getTime() - 24 * 60 * 60 * 1000);
  const effectiveEnd = end > trainEndLimit ? trainEndLimit : end;
  const durationMs = effectiveEnd.getTime() - start.getTime();
  if (!Number.isFinite(durationMs) || durationMs <= 0) return null;
  return {
    train_start_date: toDateOnly(start),
    train_end_date: toDateOnly(effectiveEnd),
  };
}

function trainingWindowForDataset(dataset) {
  return trainingWindowForDateRange(dataset?.start_at, dataset?.end_at);
}

function mapPreviewUrl(latitude, longitude) {
  const lat = Number(latitude);
  const lon = Number(longitude);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return "";
  const delta = 0.035;
  const bbox = [
    lon - delta,
    lat - delta,
    lon + delta,
    lat + delta,
  ].join(",");
  return `https://www.openstreetmap.org/export/embed.html?bbox=${bbox}&layer=mapnik&marker=${lat},${lon}`;
}

function StatusBadge({ value }) {
  return <span className={`badge badge-${value}`}>{value || "unknown"}</span>;
}

function trafficState(value) {
  if (value >= 5200) return { label: "Heavy", tone: "heavy" };
  if (value >= 3600) return { label: "Busy", tone: "busy" };
  if (value >= 1800) return { label: "Moderate", tone: "moderate" };
  return { label: "Light", tone: "light" };
}

function trafficCondition(value) {
  if (value >= 5200) return { label: "Heavy", icon: "🚦", tone: "heavy" };
  if (value >= 3600) return { label: "Busy", icon: "🚗", tone: "busy" };
  if (value >= 1800) return { label: "Moderate", icon: "🚙", tone: "moderate" };
  return { label: "Light", icon: "🛣️", tone: "light" };
}

function roundToNextHour(date = new Date()) {
  const next = new Date(date);
  next.setMinutes(0, 0, 0);
  next.setHours(next.getHours() + 1);
  return next;
}

function addHours(date, hours) {
  const next = new Date(date);
  next.setHours(next.getHours() + hours);
  return next;
}

function formatHour(date) {
  return new Intl.DateTimeFormat("en", { hour: "2-digit", minute: "2-digit" }).format(date);
}

function isSameHour(left, right) {
  return left.getFullYear() === right.getFullYear()
    && left.getMonth() === right.getMonth()
    && left.getDate() === right.getDate()
    && left.getHours() === right.getHours();
}

function formatDayLabel(date, index) {
  if (index === 0) return "Today";
  if (index === 1) return "Tomorrow";
  return new Intl.DateTimeFormat("en", { weekday: "short", month: "short", day: "2-digit" }).format(date);
}

function densityColor(value, maxValue) {
  const ratio = Math.max(0, Math.min(1, maxValue ? value / maxValue : 0));
  const stops = [
    [0, [34, 197, 94]],
    [0.38, [132, 204, 22]],
    [0.58, [250, 204, 21]],
    [0.78, [249, 115, 22]],
    [1, [239, 68, 68]],
  ];
  const upperIndex = stops.findIndex(([stop]) => ratio <= stop);
  const [startStop, startColor] = stops[Math.max(0, upperIndex - 1)];
  const [endStop, endColor] = stops[upperIndex < 0 ? stops.length - 1 : upperIndex];
  const span = Math.max(0.001, endStop - startStop);
  const localRatio = Math.max(0, Math.min(1, (ratio - startStop) / span));
  const [red, green, blue] = startColor.map((channel, index) => (
    Math.round(channel + (endColor[index] - channel) * localRatio)
  ));
  return `rgb(${red}, ${green}, ${blue})`;
}

function Card({ title, children, action }) {
  return (
    <section className="card">
      <div className="card-header">
        <h2>{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

function RegionDropdown({ regions, selectedRegion, onChange }) {
  const [open, setOpen] = useState(false);
  const dropdownRef = useRef(null);

  useEffect(() => {
    function handlePointerDown(event) {
      if (!dropdownRef.current?.contains(event.target)) setOpen(false);
    }
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, []);

  if (!regions.length) {
    return <div className="region-dropdown region-dropdown-empty">No regions</div>;
  }

  return (
    <div className={`region-dropdown ${open ? "region-dropdown-open" : ""}`} ref={dropdownRef}>
      <button
        type="button"
        className="region-dropdown-trigger"
        onClick={() => setOpen((value) => !value)}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span>{selectedRegion?.name || "Select region"}</span>
        <Icon name="chevron" />
      </button>
      {open && (
        <div className="region-dropdown-menu" role="listbox">
          {regions.map((region) => (
            <button
              type="button"
              className={region.id === selectedRegion?.id ? "region-dropdown-option active" : "region-dropdown-option"}
              key={region.id}
              onClick={() => {
                onChange(region.id);
                setOpen(false);
              }}
              role="option"
              aria-selected={region.id === selectedRegion?.id}
            >
              <span>{region.name}</span>
              {region.id === selectedRegion?.id && <small>Selected</small>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function TrafficStateIcon({ tone }) {
  const cars = { light: 1, moderate: 2, busy: 3, heavy: 4 }[tone] || 1;
  const common = {
    viewBox: "0 0 64 42",
    role: "img",
    "aria-hidden": "true",
    className: `traffic-state-icon traffic-state-icon-${tone}`,
  };

  return (
    <svg {...common}>
      {Array.from({ length: cars }).map((_, index) => {
        const x = 8 + index * 13;
        const y = index % 2 === 0 ? 16 : 6;
        return (
          <g className="traffic-car" key={index} transform={`translate(${x} ${y})`}>
            <path d="M3 12h18l-2-6H5l-2 6Z" />
            <path d="M7 6l2-3h6l2 3" />
            <circle cx="7" cy="15" r="2" />
            <circle cx="17" cy="15" r="2" />
          </g>
        );
      })}
    </svg>
  );
}

function AuthPage({ onLogin }) {
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("admin@example.com");
  const [password, setPassword] = useState("admin123456");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      if (mode === "register") {
        await api.register(email, password);
      }
      const result = await api.login(email, password);
      setStoredToken(result.access_token);
      await onLogin();
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="auth-page">
      <form className="auth-card" onSubmit={submit}>
        <p className="eyebrow">Adaptive MLOps</p>
        <h1>Traffic Management Website</h1>
        <p className="muted">
          Login to manage regions, datasets, models and traffic predictions.
        </p>
        <label>
          Email
          <input value={email} onChange={(event) => setEmail(event.target.value)} />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        {error && <div className="error">{error}</div>}
        <button disabled={loading}>{loading ? "Working..." : mode === "login" ? "Login" : "Register"}</button>
        <button
          className="link-button"
          type="button"
          onClick={() => setMode(mode === "login" ? "register" : "login")}
        >
          {mode === "login" ? "Create user account" : "Back to login"}
        </button>
      </form>
    </main>
  );
}

function Dashboard({ regions, selectedRegion, datasets, models, user }) {
  const activeModel = models.find((item) => item.id === selectedRegion?.active_model_version_id);
  const activeModelText = activeModel
    ? `${activeModel.version} (${activeModel.variant})`
    : selectedRegion?.active_model_version && selectedRegion?.active_model_variant
      ? `${selectedRegion.active_model_version} (${selectedRegion.active_model_variant})`
      : "";
  const readyDatasets = datasets.filter((item) => item.status === "valid").length;
  const [hourlyForecasts, setHourlyForecasts] = useState([]);
  const [dailyForecasts, setDailyForecasts] = useState([]);
  const [selectedDayIndex, setSelectedDayIndex] = useState(0);
  const [dailyMode, setDailyMode] = useState("list");
  const [loadingForecasts, setLoadingForecasts] = useState(false);
  const [forecastError, setForecastError] = useState("");
  const [highlightedHourIndex, setHighlightedHourIndex] = useState(null);
  const hourlyStripRef = useRef(null);
  const hourCardRefs = useRef([]);

  useEffect(() => {
    if (!selectedRegion?.id || !selectedRegion.active_model_version_id) {
      setHourlyForecasts([]);
      setDailyForecasts([]);
      return;
    }

    let cancelled = false;
    async function loadForecasts() {
      setLoadingForecasts(true);
      setForecastError("");
      try {
        const dashboard = await api.forecastDashboard(selectedRegion.id);
        const hourlyResults = (dashboard.hourly_24h || []).map((point) => {
          const value = Math.round(point.prediction);
          return {
            date: new Date(point.forecast_for),
            value,
            condition: trafficState(value),
          };
        });
        const dailyResults = (dashboard.daily_7d || []).map((day) => {
          const min = Math.round(day.min_prediction);
          const max = Math.round(day.max_prediction);
          const avg = Math.round(day.avg_prediction);
          return {
            date: new Date(day.date),
            min,
            max,
            avg,
            condition: trafficState(avg),
            points: (day.points || []).map((point) => ({
              date: new Date(point.forecast_for),
              value: Math.round(point.prediction),
            })),
          };
        });

        if (!cancelled) {
          setHourlyForecasts(hourlyResults);
          setDailyForecasts(dailyResults);
          setSelectedDayIndex(0);
          setDailyMode("list");
        }
      } catch (err) {
        if (!cancelled) {
          setHourlyForecasts([]);
          setDailyForecasts([]);
          setForecastError(err.message);
        }
      } finally {
        if (!cancelled) setLoadingForecasts(false);
      }
    }

    loadForecasts();
    return () => {
      cancelled = true;
    };
  }, [selectedRegion?.id, selectedRegion?.active_model_version_id]);

  const selectedDay = dailyForecasts[selectedDayIndex];
  const strongestHour = hourlyForecasts.reduce((best, item) => (
    !best || item.value > best.value ? item : best
  ), null);
  const strongestHourIndex = strongestHour
    ? hourlyForecasts.findIndex((item) => item.date.getTime() === strongestHour.date.getTime())
    : -1;
  const dailyScaleMax = Math.max(1, ...dailyForecasts.map((item) => item.max));

  useEffect(() => {
    const strip = hourlyStripRef.current;
    if (!strip) return undefined;

    function handleWheel(event) {
      if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;
      strip.scrollLeft += event.deltaY;
      event.preventDefault();
      event.stopPropagation();
    }

    strip.addEventListener("wheel", handleWheel, { passive: false });
    return () => strip.removeEventListener("wheel", handleWheel);
  }, []);

  function scrollToPeakHour() {
    if (strongestHourIndex < 0) return;
    setHighlightedHourIndex(strongestHourIndex);
    hourCardRefs.current[strongestHourIndex]?.scrollIntoView({
      behavior: "smooth",
      block: "nearest",
      inline: "center",
    });
    window.setTimeout(() => setHighlightedHourIndex(null), 1600);
  }

  return (
    <div className="forecast-dashboard">
      {forecastError && <div className="alert alert-error">{forecastError}</div>}

      <section className="weather-panel hourly-panel">
        <div className="weather-panel-header">
          <div>
            <span className="panel-label">Next 24 Hours</span>
            <h3>Hourly traffic outlook</h3>
          </div>
          <div className="forecast-panel-actions">
            {activeModelText && <span className="forecast-chip">{activeModelText}</span>}
            <button type="button" className="forecast-chip forecast-chip-peak" onClick={scrollToPeakHour}>
              <strong>{strongestHour ? strongestHour.value.toLocaleString() : "-"}</strong>
              Peak hour
            </button>
            {loadingForecasts && <span className="forecast-loading">Updating...</span>}
          </div>
        </div>
        <div className="hourly-strip" tabIndex={0} ref={hourlyStripRef}>
          {hourlyForecasts.length ? hourlyForecasts.map((item, index) => (
            <div
              className={`hour-card hour-card-${item.condition.tone} ${
                highlightedHourIndex === index ? "hour-card-peak-highlight" : ""
              }`}
              key={item.date.toISOString()}
              ref={(element) => { hourCardRefs.current[index] = element; }}
            >
              <span className="hour-time">{isSameHour(item.date, new Date()) ? "Now" : formatHour(item.date)}</span>
              <TrafficStateIcon tone={item.condition.tone} />
              <b>{item.value.toLocaleString()}</b>
              <small>{item.condition.label}</small>
            </div>
          )) : (
            <p className="muted">No hourly forecast available.</p>
          )}
        </div>
      </section>

      <section className="weather-panel daily-panel">
        <div className="weather-panel-header">
          <div>
            <span className="panel-label">{dailyMode === "chart" ? "Daily Detail" : "7-Day Forecast"}</span>
            <h3>{dailyMode === "chart" && selectedDay ? formatDayLabel(selectedDay.date, selectedDayIndex) : "Daily traffic range"}</h3>
          </div>
          {dailyMode === "chart" && (
            <button type="button" className="secondary compact-button" onClick={() => setDailyMode("list")}>
              Back to 7 days
            </button>
          )}
        </div>

        {dailyMode === "list" ? (
          <div className="daily-list">
            {dailyForecasts.length ? dailyForecasts.map((item, index) => (
              <button
                type="button"
                className={`daily-row ${selectedDayIndex === index ? "daily-row-active" : ""}`}
                key={item.date.toISOString()}
                onClick={() => {
                  setSelectedDayIndex(index);
                  setDailyMode("chart");
                }}
              >
                <span>{formatDayLabel(item.date, index)}</span>
                <TrafficStateIcon tone={item.condition.tone} />
                <span className="daily-range">
                  <em>{item.min.toLocaleString()}</em>
                  <i
                    style={{
                      "--range-start": `${Math.max(0, Math.min(100, (item.min / dailyScaleMax) * 100))}%`,
                      "--range-width": `${Math.max(6, Math.min(100, ((item.max - item.min) / dailyScaleMax) * 100))}%`,
                      "--density-low": densityColor(item.min, dailyScaleMax),
                      "--density-high": densityColor(item.max, dailyScaleMax),
                    }}
                  />
                  <strong>{item.max.toLocaleString()}</strong>
                </span>
              </button>
            )) : (
              <p className="muted">No daily forecast available.</p>
            )}
          </div>
        ) : selectedDay ? (
            <div className="daily-detail">
              <div className="daily-detail-summary">
                <div>
                  <strong>{selectedDay.avg.toLocaleString()}</strong>
                  <span>average vehicles/hour</span>
                </div>
                <div className={`condition-pill condition-${selectedDay.condition.tone}`}>
                  <TrafficStateIcon tone={selectedDay.condition.tone} /> {selectedDay.condition.label}
                </div>
                <div>
                  <strong>{selectedDay.min.toLocaleString()} - {selectedDay.max.toLocaleString()}</strong>
                  <span>daily range</span>
                </div>
              </div>
              <TrafficChart points={selectedDay.points} />
            </div>
        ) : (
            <p className="muted">Select a forecast day to see the traffic curve.</p>
        )}
      </section>
    </div>
  );
}

function TrafficChart({ points }) {
  if (!points?.length) return null;
  const width = 720;
  const height = 260;
  const padding = 28;
  const values = points.map((point) => point.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(1, max - min);
  const coords = points.map((point, index) => {
    const x = padding + (index / (points.length - 1)) * (width - padding * 2);
    const y = height - padding - ((point.value - min) / range) * (height - padding * 2);
    return { x, y, point };
  });
  const line = coords.map((coord) => `${coord.x},${coord.y}`).join(" ");
  const area = `${padding},${height - padding} ${line} ${width - padding},${height - padding}`;
  const peak = coords.reduce((best, coord) => (coord.point.value > best.point.value ? coord : best), coords[0]);

  return (
    <div className="traffic-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Traffic volume chart">
        <defs>
          <linearGradient id="trafficArea" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="#fb923c" stopOpacity="0.6" />
            <stop offset="100%" stopColor="#facc15" stopOpacity="0.18" />
          </linearGradient>
        </defs>
        {[0, 1, 2, 3].map((lineIndex) => {
          const y = padding + lineIndex * ((height - padding * 2) / 3);
          return <line key={lineIndex} x1={padding} x2={width - padding} y1={y} y2={y} className="chart-grid-line" />;
        })}
        <polygon points={area} className="chart-area" />
        <polyline points={line} className="chart-line" />
        <circle cx={peak.x} cy={peak.y} r="6" className="chart-peak" />
      </svg>
      <div className="chart-axis">
        <span>00:00</span>
        <span>06:00</span>
        <span>12:00</span>
        <span>18:00</span>
        <span>23:00</span>
      </div>
      <div className="chart-summary">
        <span>Low {min.toLocaleString()}</span>
        <strong>Peak {peak.point.value.toLocaleString()} vehicles/hour</strong>
        <span>High {max.toLocaleString()}</span>
      </div>
    </div>
  );
}

function RegionManager({ regions, selectedRegion, refresh }) {
  const [lookupQuery, setLookupQuery] = useState("");
  const [lookingUp, setLookingUp] = useState(false);
  const [form, setForm] = useState({
    name: "",
    description: "",
    latitude: "10.776900",
    longitude: "106.700900",
    timezone: "Asia/Ho_Chi_Minh",
  });
  const [message, setMessage] = useState("");
  const mapUrl = mapPreviewUrl(form.latitude, form.longitude);

  useEffect(() => {
    if (!selectedRegion) return;
    setForm({
      name: selectedRegion.name || "",
      description: selectedRegion.description || "",
      latitude: String(selectedRegion.latitude ?? ""),
      longitude: String(selectedRegion.longitude ?? ""),
      timezone: selectedRegion.timezone || "",
    });
    setLookupQuery("");
  }, [selectedRegion?.id]);

  async function createRegion(event) {
    event.preventDefault();
    setMessage("");
    try {
      await api.createRegion({
        ...form,
        latitude: Number(form.latitude),
        longitude: Number(form.longitude),
      });
      setForm({ ...form, name: "", description: "" });
      await refresh();
      setMessage("Region created.");
    } catch (err) {
      setMessage(err.message);
    }
  }

  async function lookupLocation() {
    if (!lookupQuery.trim()) return;
    setMessage("");
    setLookingUp(true);
    try {
      const result = await api.lookupRegion(lookupQuery);
      setForm({
        name: result.name,
        description: result.display_name,
        latitude: result.latitude,
        longitude: result.longitude,
        timezone: result.timezone,
      });
    } catch (err) {
      setMessage(err.message);
    } finally {
      setLookingUp(false);
    }
  }

  async function toggleRegion(region) {
    await api.updateRegion(region.id, { is_active: !region.is_active });
    await refresh();
  }

  async function deleteRegion(region) {
    if (!window.confirm(`Permanently delete "${region.name}" and all related datasets, models, training runs and predictions?`)) {
      return;
    }
    await api.deleteRegion(region.id);
    await refresh();
  }

  function activeModelLabel(region) {
    if (region.active_model_version && region.active_model_variant) {
      return `${region.active_model_version} (${region.active_model_variant})`;
    }
    return region.active_model_version_id || "-";
  }

  return (
    <div className="region-layout">
      <Card title="Create Region">
        <form className="form" onSubmit={createRegion}>
          <div className="region-create-layout">
            <div className="region-form-panel">
              <div className="lookup-row">
                <input
                  placeholder="Search city or place, e.g. Ho Chi Minh City"
                  value={lookupQuery}
                  onChange={(e) => setLookupQuery(e.target.value)}
                />
                <button
                  type="button"
                  className="secondary"
                  disabled={lookingUp || !lookupQuery.trim()}
                  onClick={lookupLocation}
                >
                  {lookingUp ? "Looking..." : "Lookup"}
                </button>
              </div>
              <div className="region-form-grid">
                <input placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
                <input placeholder="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
                <input placeholder="Latitude" value={form.latitude} onChange={(e) => setForm({ ...form, latitude: e.target.value })} />
                <input placeholder="Longitude" value={form.longitude} onChange={(e) => setForm({ ...form, longitude: e.target.value })} />
                <input placeholder="Timezone" value={form.timezone} onChange={(e) => setForm({ ...form, timezone: e.target.value })} />
              </div>
              <button>Create</button>
              {message && <p className="muted">{message}</p>}
            </div>
            {mapUrl && (
              <div className="map-preview region-map-preview">
                <iframe
                  title="Region map preview"
                  src={mapUrl}
                  loading="lazy"
                  referrerPolicy="no-referrer-when-downgrade"
                />
              </div>
            )}
          </div>
        </form>
      </Card>
      <Card title="Regions">
        <Table
          columns={["Name", "Timezone", "Status", "Active Model", "Action"]}
          rows={regions.map((region) => [
            region.name,
            region.timezone,
            <StatusBadge value={region.is_active ? "active" : "inactive"} />,
            activeModelLabel(region),
            <div className="row-actions">
              <label className="switch-control" title={region.is_active ? "Disable region" : "Enable region"}>
                <input
                  type="checkbox"
                  checked={region.is_active}
                  onChange={() => toggleRegion(region)}
                />
                <span />
              </label>
              <button className="icon-button danger-icon" title="Delete region" onClick={() => deleteRegion(region)}>
                <Icon name="trash" />
              </button>
            </div>,
          ])}
        />
      </Card>
    </div>
  );
}

function DatasetManager({ selectedRegion, datasets, refreshDatasets, refreshModels, refreshRegions }) {
  const [file, setFile] = useState(null);
  const [dragActive, setDragActive] = useState(false);
  const [message, setMessage] = useState("");
  const [messageType, setMessageType] = useState("info");
  const [uploading, setUploading] = useState(false);
  const [activeTraining, setActiveTraining] = useState(null);
  const [trainingDataset, setTrainingDataset] = useState(null);
  const [trainForm, setTrainForm] = useState({
    train_start_date: "",
    train_end_date: "",
    artifact_root: "models/regions",
    model_role: "candidate",
    cv_splits: 3,
    random_state: 42,
    recurrent_sequence_length: 72,
    recurrent_epochs: 3,
    recurrent_batch_size: 32,
    final_test_ratio: 0.15,
  });

  useEffect(() => {
    const latestValidDataset = datasets.find((dataset) => dataset.status === "valid");
    const window = trainingWindowForDataset(latestValidDataset);
    if (!window) return;
    setTrainForm((current) => ({
      ...current,
      ...window,
    }));
  }, [selectedRegion?.id, datasets]);

  useEffect(() => {
    if (!activeTraining?.id || ["completed", "failed"].includes(activeTraining.status)) {
      return undefined;
    }

    const timer = window.setInterval(async () => {
      try {
        const trainingRun = await api.trainingRun(activeTraining.id);
        setActiveTraining((current) => ({
          ...(current || {}),
          ...trainingRun,
          datasetName: current?.datasetName,
        }));
        if (trainingRun.status === "completed") {
          setMessage("");
          await Promise.all([refreshDatasets(), refreshModels(), refreshRegions()]);
        }
        if (trainingRun.status === "failed") {
          setMessageType("error");
          setMessage("Training failed. Please check the dataset and try again.");
        }
      } catch (err) {
        setMessageType("error");
        setMessage("Could not refresh training status. Please try again.");
      }
    }, 4000);

    return () => window.clearInterval(timer);
  }, [activeTraining?.id, activeTraining?.status, refreshDatasets, refreshModels]);

  function chooseFile(nextFile) {
    setMessage("");
    if (!nextFile) {
      setFile(null);
      return;
    }
    if (!nextFile.name.toLowerCase().endsWith(".csv")) {
      setFile(null);
      setMessageType("error");
      setMessage("Only CSV files are supported.");
      return;
    }
    setFile(nextFile);
    updateTrainingWindowFromFile(nextFile);
  }

  async function updateTrainingWindowFromFile(nextFile) {
    try {
      const text = await nextFile.text();
      const lines = text.split(/\r?\n/).filter((line) => line.trim());
      if (lines.length < 2) return;
      const headers = lines[0].split(",").map((header) => header.trim());
      const dateIndex = headers.indexOf("date_time");
      if (dateIndex < 0) return;
      const firstDate = lines[1].split(",")[dateIndex];
      const lastDate = lines[lines.length - 1].split(",")[dateIndex];
      const window = trainingWindowForDateRange(firstDate, lastDate);
      if (!window) return;
      setTrainForm((current) => ({
        ...current,
        ...window,
      }));
    } catch {
      // Backend validation remains authoritative after upload.
    }
  }

  function handleDrag(event, active) {
    event.preventDefault();
    event.stopPropagation();
    setDragActive(active);
  }

  function handleDrop(event) {
    handleDrag(event, false);
    chooseFile(event.dataTransfer.files?.[0] || null);
  }

  async function upload(event) {
    event.preventDefault();
    if (!selectedRegion || !file) return;
    setMessage("");
    setUploading(true);
    try {
      const result = await api.uploadDataset(selectedRegion.id, file);
      const window = trainingWindowForDataset(result.dataset);
      if (window) {
        setTrainForm((current) => ({
          ...current,
          ...window,
        }));
      }
      await refreshDatasets();
      setMessageType("success");
      setMessage(`Uploaded ${result.dataset.original_filename}: ${result.dataset.status}`);
      setFile(null);
    } catch (err) {
      setMessageType("error");
      setMessage(err.message);
    } finally {
      setUploading(false);
    }
  }

  async function train(dataset, selectedModels = defaultTrainingModels, trainingConfigOverride = null) {
    setMessage("");
    try {
      const window = trainingWindowForDataset(dataset);
      const trainingConfig = {
        ...trainForm,
        ...(window || {}),
        ...(trainingConfigOverride || {}),
      };
      if (!trainingConfig.train_start_date || !trainingConfig.train_end_date) {
        throw new Error("Dataset date range is unavailable. Upload a valid dataset before training.");
      }
      const result = await api.trainDataset(dataset.id, {
        ...trainingConfig,
        cv_splits: Number(trainingConfig.cv_splits),
        random_state: Number(trainingConfig.random_state),
        recurrent_sequence_length: Number(trainingConfig.recurrent_sequence_length || 72),
        recurrent_epochs: Number(trainingConfig.recurrent_epochs || 3),
        recurrent_batch_size: Number(trainingConfig.recurrent_batch_size || 32),
        final_test_ratio: Number(trainingConfig.final_test_ratio || 0.15),
        selected_models: selectedModels,
      });
      setActiveTraining({
        ...result.training_run,
        datasetName: dataset.original_filename,
      });
      setMessage("");
    } catch (err) {
      setMessageType("error");
      setMessage(err.message);
      throw err;
    }
  }

  function requestTraining(dataset) {
    setTrainingDataset(dataset);
  }

  return (
    <div className="grid">
      <div className="grid two">
        <Card title="Upload Dataset CSV">
          <form className="form" onSubmit={upload}>
            <label
              className={`dropzone ${dragActive ? "dropzone-active" : ""} ${file ? "dropzone-ready" : ""}`}
              onDragEnter={(event) => handleDrag(event, true)}
              onDragOver={(event) => handleDrag(event, true)}
              onDragLeave={(event) => handleDrag(event, false)}
              onDrop={handleDrop}
            >
              <input
                className="file-input"
                type="file"
                accept=".csv,text/csv"
                onChange={(event) => chooseFile(event.target.files?.[0] || null)}
              />
              <span className="dropzone-icon">CSV</span>
              <strong>{file ? file.name : "Drop your dataset here"}</strong>
              <span>
                {file
                  ? `${(file.size / 1024 / 1024).toFixed(2)} MB selected`
                  : "or click to browse a training CSV file"}
              </span>
              <small>
                Required schema follows traffic-training-csv/v1. The backend will
                validate rows, dates and quality before training.
              </small>
            </label>
            {file && (
              <div className="file-preview">
                <div>
                  <strong>{file.name}</strong>
                  <span>{(file.size / 1024).toFixed(1)} KB</span>
                </div>
                <button type="button" className="secondary" onClick={() => chooseFile(null)}>
                  Clear
                </button>
              </div>
            )}
            <button disabled={!selectedRegion || !file || uploading}>
              {uploading ? "Uploading..." : "Upload and validate"}
            </button>
          </form>
          {message && <div className={`alert alert-${messageType}`}>{message}</div>}
          {activeTraining && (
            <div className={`training-status training-status-${activeTraining.status}`}>
              {!["completed", "failed"].includes(activeTraining.status) && <span className="spinner" />}
              <div>
                <strong>
                  {activeTraining.status === "completed"
                    ? "Training completed"
                    : activeTraining.status === "failed"
                      ? "Training failed"
                      : "Training in progress"}
                </strong>
                <p>
                  {activeTraining.datasetName || "Dataset"} · status: {activeTraining.status}
                </p>
                {activeTraining.airflow_dag_run_id && (
                  <small>Airflow run: {activeTraining.airflow_dag_run_id}</small>
                )}
                {activeTraining.error_message && <small>{activeTraining.error_message}</small>}
              </div>
            </div>
          )}
        </Card>
        <Card title="Training Configuration">
          <p className="muted">
            These values are used when you click Train on a valid dataset.
          </p>
          <div className="form-grid">
            {Object.entries(trainForm).map(([key, value]) => (
              <label key={key}>
                {key}
                <input value={value} onChange={(e) => setTrainForm({ ...trainForm, [key]: e.target.value })} />
              </label>
            ))}
          </div>
        </Card>
      </div>
      <Card title="Datasets">
        <Table
          columns={["File", "Rows", "Date Range", "Status", "Action"]}
          rows={datasets.map((dataset) => [
            dataset.original_filename,
            dataset.row_count || "-",
            `${formatDate(dataset.start_at)} - ${formatDate(dataset.end_at)}`,
            <StatusBadge value={dataset.status} />,
            <TrainingAction dataset={dataset} activeTraining={activeTraining} onTrain={requestTraining} />,
          ])}
        />
      </Card>
      {trainingDataset && (
        <TrainModelModal
          dataset={trainingDataset}
          trainForm={trainForm}
          onClose={() => setTrainingDataset(null)}
          onSubmit={async (dataset, selectedModels, modalTrainForm) => {
            await train(dataset, selectedModels, modalTrainForm);
            setTrainingDataset(null);
          }}
        />
      )}
    </div>
  );
}

function TrainModelModal({
  dataset,
  trainForm,
  initialSelectedModels = defaultTrainingModels,
  title = "Select models",
  eyebrow = "Training",
  submitVerb = "Train",
  onClose,
  onSubmit,
}) {
  const [selectedModels, setSelectedModels] = useState(initialSelectedModels);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  function toggleModel(modelId) {
    setSelectedModels((current) => (
      current.includes(modelId)
        ? current.filter((item) => item !== modelId)
        : [...current, modelId]
    ));
  }

  async function submit(event) {
    event.preventDefault();
    setError("");
    if (!selectedModels.length) {
      setError("Select at least one model.");
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit(dataset, selectedModels, trainForm);
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  const treeCount = selectedModels.filter((model) => model.endsWith("_lag")).length;
  const neuralCount = selectedModels.filter((model) => ["lstm", "gru"].includes(model)).length;

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <form className="modal-card train-modal-card" onSubmit={submit} onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <div>
            <p className="eyebrow">{eyebrow}</p>
            <h2>{title}</h2>
          </div>
          <button className="secondary" type="button" onClick={onClose}>Close</button>
        </div>

        <div className="train-modal-dataset">
          <strong>{dataset.original_filename}</strong>
          <span>Training window: {trainForm.train_start_date || "-"} - {trainForm.train_end_date || "-"}</span>
        </div>

        <div className="model-picker">
          {trainingModelOptions.map((option) => (
            <label className="model-option" key={option.id}>
              <input
                type="checkbox"
                checked={selectedModels.includes(option.id)}
                onChange={() => toggleModel(option.id)}
              />
              <span>
                <strong>{option.label}</strong>
                <small>{option.group}</small>
              </span>
            </label>
          ))}
        </div>

        <div className="fairness-panel">
          <div>
            <span>CV folds</span>
            <strong>{trainForm.cv_splits}</strong>
          </div>
          <div>
            <span>Final test</span>
            <strong>{Number(trainForm.final_test_ratio || 0.15) * 100}%</strong>
          </div>
          <div>
            <span>Parallel branches</span>
            <strong>{treeCount ? "tree" : "-"} | {neuralCount ? "neural" : "-"}</strong>
          </div>
        </div>

        <p className="muted">
          Selection uses the same train window, expanding-window CV folds, and CV mean MAE for every selected model. Final Test is reported only.
        </p>

        {error && <div className="alert alert-error">{error}</div>}
        <button disabled={submitting || !selectedModels.length}>
          {submitting ? "Triggering..." : `${submitVerb} ${selectedModels.length} model${selectedModels.length > 1 ? "s" : ""}`}
        </button>
      </form>
    </div>
  );
}

function TrainingAction({ dataset, activeTraining, onTrain }) {
  const isThisDataset = activeTraining?.dataset_id === dataset.id;
  const isRunning = isThisDataset && !["completed", "failed"].includes(activeTraining.status);
  const isCompleted = isThisDataset && activeTraining.status === "completed";
  const isFailed = isThisDataset && activeTraining.status === "failed";
  const hasRunningTraining = activeTraining && !["completed", "failed"].includes(activeTraining.status);

  if (isCompleted) {
    return <span className="inline-status inline-status-success">Model ready</span>;
  }

  if (isFailed) {
    return (
      <button className="danger" onClick={() => onTrain(dataset)}>
        Retry
      </button>
    );
  }

  return (
    <button
      className={isRunning ? "button-loading" : ""}
      disabled={dataset.status !== "valid" || hasRunningTraining}
      onClick={() => onTrain(dataset)}
    >
      {isRunning && <span className="button-spinner" />}
      {isRunning ? "Training" : "Train"}
    </button>
  );
}

function modelComparisonRows(model) {
  const rows = model.model_comparison || model.training_configuration?.model_comparison || [];
  return rows.length
    ? rows
    : [{
      model_name: model.variant,
      validation_MAE: model.cv_mean_mae,
      validation_MAE_std: model.cv_std_mae,
      test_MAE: model.final_test_mae,
      inference_supported: true,
    }];
}

function metricText(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : "-";
}

function modelDisplayName(value) {
  const normalized = String(value || "").toLowerCase();
  const labels = {
    random_forest_lag: "Random Forest",
    random_forest: "Random Forest",
    randomforest: "Random Forest",
    xgboost_lag: "XGBoost",
    xgboost: "XGBoost",
    lightgbm_lag: "LightGBM",
    lightgbm: "LightGBM",
    lstm: "LSTM",
    gru: "GRU",
  };
  if (labels[normalized]) return labels[normalized];
  return String(value || "-").replace(/_lag$/i, "").replace(/_/g, " ");
}

function selectedModelSummary(configuration) {
  const selected = configuration?.selected_models || configuration?.selected_from_candidates || [];
  return selected.length ? selected.map(modelDisplayName).join(", ") : "-";
}

function comparisonRole(item, parentModel) {
  if (item.selected || item.model_name === parentModel.variant) return "Selected";
  if (item.inference_supported === false || item.benchmark_only) return "Benchmark";
  return "Candidate";
}

function isDriftRetrainedModel(model) {
  return model.training_configuration?.trigger_source === "feature_drift";
}

function ModelManager({ selectedRegion, models, refreshModels, refreshRegions }) {
  const [expandedModelId, setExpandedModelId] = useState("");

  async function activate(id) {
    await api.activateModel(id);
    await Promise.all([refreshModels(), refreshRegions()]);
  }

  async function deleteModel(model) {
    if (!window.confirm(`Permanently delete ${model.version} (${model.variant}) and related predictions?`)) {
      return;
    }
    await api.deleteModel(model.id);
    await Promise.all([refreshModels(), refreshRegions()]);
  }

  return (
    <Card title="Model Versions">
      {models.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Version</th>
                <th>Variant</th>
                <th>CV MAE</th>
                <th>Test MAE</th>
                <th>Status</th>
                <th>Created</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {models.map((model) => {
                const expanded = expandedModelId === model.id;
                return (
                  <React.Fragment key={model.id}>
                    <tr>
                      <td>{model.version}</td>
                      <td>{model.variant}</td>
                      <td>{metricText(model.cv_mean_mae)}</td>
                      <td>{metricText(model.final_test_mae)}</td>
                      <td>
                        <span className="status-cell">
                          <StatusBadge value={model.status} />
                          {isDriftRetrainedModel(model) && (
                            <span
                              className="drift-retrain-indicator"
                              title="Retrained after feature drift"
                            >
                              <Icon name="drift-retrain" />
                            </span>
                          )}
                        </span>
                      </td>
                      <td>{formatDate(model.created_at)}</td>
                      <td>
                        <div className="row-actions">
                          <button
                            className="secondary compact-button"
                            type="button"
                            onClick={() => setExpandedModelId(expanded ? "" : model.id)}
                          >
                            {expanded ? "Hide" : "Details"}
                          </button>
                          <button disabled={model.id === selectedRegion?.active_model_version_id} onClick={() => activate(model.id)}>
                            Activate
                          </button>
                          <button className="danger" onClick={() => deleteModel(model)}>Delete</button>
                        </div>
                      </td>
                    </tr>
                    {expanded && (
                      <tr className="model-details-row">
                        <td colSpan={7}>
                          <div className="model-comparison-panel">
                            <div className="model-comparison-meta">
                              <span>Training window: <strong>{model.training_configuration?.train_start_date || "-"} - {model.training_configuration?.train_end_date || "-"}</strong></span>
                              <span>Selected: <strong>{selectedModelSummary(model.training_configuration)}</strong></span>
                            </div>
                            <table className="nested-table">
                              <thead>
                                <tr>
                                  <th>Model</th>
                                  <th>CV MAE</th>
                                  <th>Test MAE</th>
                                  <th>Role</th>
                                </tr>
                              </thead>
                              <tbody>
                                {modelComparisonRows(model).map((item, index) => (
                                  <tr key={`${item.model_name || "model"}-${index}`}>
                                    <td>{modelDisplayName(item.model_name)}</td>
                                    <td>{metricText(item.validation_MAE)}</td>
                                    <td>{metricText(item.test_MAE)}</td>
                                    <td>{comparisonRole(item, model)}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">No data yet.</p>
      )}
    </Card>
  );
}

function PredictionPage({ selectedRegion }) {
  const [forecastFor, setForecastFor] = useState("");
  const [predictionWindow, setPredictionWindow] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [loadingWindow, setLoadingWindow] = useState(false);

  useEffect(() => {
    if (!selectedRegion?.id) return;
    setError("");
    setResult(null);
    setPredictionWindow(null);
    setForecastFor("");
    setLoadingWindow(true);
    api.predictionWindow(selectedRegion.id)
      .then((window) => {
        setPredictionWindow(window);
        const start = currentForecastStart(window);
        setForecastFor(start ? toDatetimeLocalFromDate(start) : isoToDatetimeLocalValue(window.production_start_at));
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoadingWindow(false));
  }, [selectedRegion?.id]);

  async function submit(event) {
    event.preventDefault();
    setError("");
    setResult(null);
    try {
      const payload = {
        forecast_for: localDatetimeToIsoWithOffset(forecastFor),
      };
      setResult(await api.predict(selectedRegion.id, payload));
    } catch (err) {
      setError(err.message);
    }
  }

  function setQuickForecast(daysFromStart) {
    if (!predictionWindow) return;
    const base = currentForecastStart(predictionWindow) || new Date(predictionWindow.production_start_at);
    const target = addDaysToIso(base, daysFromStart);
    const clamped = clampDate(
      target,
      predictionWindow.production_start_at,
      predictionWindow.production_end_at,
    );
    setForecastFor(toDatetimeLocalFromDate(clamped));
  }

  const usedFeatures = result?.prediction?.feature_snapshot_json || null;
  const featureRows = usedFeatures
    ? Object.entries(usedFeatures)
      .filter(([key]) => !["model_features", "feature_source", "production_window"].includes(key))
      .map(([key, value]) => [key, value === null || value === undefined ? "-" : String(value)])
    : [];

  return (
    <div className="grid two">
      <Card title="Predict Traffic">
        <form className="form" onSubmit={submit}>
          <div className="readonly-field">{selectedRegion?.name || "Select a region"}</div>
          {loadingWindow && <div className="alert alert-info">Loading production window...</div>}
          <label>
            Forecast time
            <input
              type="datetime-local"
              min={isoToDatetimeLocalValue(predictionWindow?.production_start_at)}
              max={isoToDatetimeLocalValue(predictionWindow?.production_end_at)}
              value={forecastFor}
              onChange={(event) => setForecastFor(event.target.value)}
            />
          </label>
          <div className="quick-actions">
            <button type="button" className="secondary" disabled={!predictionWindow} onClick={() => setQuickForecast(0)}>
              Start
            </button>
            <button type="button" className="secondary" disabled={!predictionWindow} onClick={() => setQuickForecast(1)}>
              Tomorrow
            </button>
            <button type="button" className="secondary" disabled={!predictionWindow} onClick={() => setQuickForecast(7)}>
              Next week
            </button>
            <button type="button" className="secondary" disabled={!predictionWindow} onClick={() => setQuickForecast(30)}>
              Next month
            </button>
          </div>
          <button disabled={!selectedRegion?.id || !forecastFor || !predictionWindow}>Predict</button>
        </form>
      </Card>
      <Card title="Prediction Result">
        {error && <div className="error">{error}</div>}
        {result ? (
          <div className="prediction-result">
            <div className="prediction-header">
              <div className="prediction-main">
                <strong>{Math.round(result.prediction.prediction).toLocaleString()}</strong>
                <span>vehicles/hour</span>
              </div>
              <div className="prediction-model">
                <span>Model</span>
                <strong>{result.model_version} ({result.model_variant})</strong>
              </div>
            </div>
            {usedFeatures && (
              <div className="feature-summary">
                <p><strong>Predicted Condition</strong></p>
                <div className="feature-grid">
                  {featureRows.map(([key, value]) => (
                    <div key={key}>
                      <span>{key}</span>
                      <strong>{value}</strong>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : <p className="muted">Submit a forecast request to see the result.</p>}
      </Card>
    </div>
  );
}

function driftStatusText(check) {
  if (!check) return "Not checked";
  if (check.status === "retrain_triggered") return "Retrain triggered";
  if (check.status === "drift_detected") return "Drift detected";
  if (check.status === "stable") return "Stable";
  if (check.status === "skipped") return "Skipped";
  if (check.status === "failed") return "Failed";
  return check.status || "Unknown";
}

function driftFeatureRows(check) {
  const features = check?.feature_drift_json?.features || {};
  return Object.entries(features)
    .map(([name, details]) => ({
      name,
      type: details.type || "-",
      metric: details.metric || "-",
      score: Number(details.value ?? 0),
      threshold: Number(details.threshold ?? 0),
      drifted: Boolean(details.drift),
    }))
    .sort((left, right) => Number(right.drifted) - Number(left.drifted) || right.score - left.score)
    .slice(0, 12);
}

function DriftMonitor({ selectedRegion, refreshModels = async () => {}, refreshRegions = async () => {} }) {
  const [checks, setChecks] = useState([]);
  const [latest, setLatest] = useState(null);
  const [selectedCheck, setSelectedCheck] = useState(null);
  const [loading, setLoading] = useState(false);
  const [checking, setChecking] = useState(false);
  const [autoRetrain, setAutoRetrain] = useState(false);
  const [currentEnd, setCurrentEnd] = useState(() => toDatetimeLocalFromDate(new Date()));
  const [productionWindow, setProductionWindow] = useState(null);
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [pendingWindowChange, setPendingWindowChange] = useState(false);
  const [retrainPlan, setRetrainPlan] = useState(null);
  const [message, setMessage] = useState("");
  const [messageType, setMessageType] = useState("info");

  async function refresh(options = {}) {
    if (!selectedRegion?.id) {
      setChecks([]);
      setLatest(null);
      setSelectedCheck(null);
      return null;
    }
    setLoading(true);
    setMessage("");
    try {
      const data = await api.driftChecks(selectedRegion.id);
      const items = data.items || [];
      const nextLatest = data.latest || null;
      setChecks(items);
      setLatest(nextLatest);
      setSelectedCheck((current) => {
        if (current && items.some((check) => check.id === current.id)) {
          return items.find((check) => check.id === current.id);
        }
        return nextLatest;
      });
      if (options.syncWindow && data.latest?.current_end_at) {
        setCurrentEnd(isoToDatetimeLocalValue(data.latest.current_end_at));
        setPendingWindowChange(false);
      }
      return data;
    } catch (err) {
      setMessageType("error");
      setMessage(err.message);
      return null;
    } finally {
      setLoading(false);
    }
  }

  async function loadProductionWindow() {
    if (!selectedRegion?.id || !selectedRegion.active_model_version_id) {
      setProductionWindow(null);
      return null;
    }
    try {
      const window = await api.predictionWindow(selectedRegion.id);
      setProductionWindow(window);
      if (window.production_end_at) {
        setCurrentEnd(isoToDatetimeLocalValue(window.production_end_at));
        setPendingWindowChange(true);
      }
      return window;
    } catch (err) {
      setProductionWindow(null);
      setMessageType("error");
      setMessage(err.message);
      return null;
    }
  }

  useEffect(() => {
    refresh({ syncWindow: true });
    loadProductionWindow();
  }, [selectedRegion?.id, selectedRegion?.active_model_version_id]);

  async function runCheck(forceRetrain = false) {
    if (!selectedRegion?.id) return;
    const requestedCurrentEnd = currentEnd;
    setChecking(true);
    setMessage("");
    setPendingWindowChange(true);
    try {
      const result = await api.runDriftCheck(
        selectedRegion.id,
        {
          autoRetrain: forceRetrain ? true : autoRetrain,
          forceRetrain,
          currentEnd: requestedCurrentEnd || undefined,
        },
      );
      const nextLatest = result.checks?.[0] || null;
      if (nextLatest) {
        setLatest(nextLatest);
        setSelectedCheck(nextLatest);
        if (nextLatest.current_end_at) {
          setCurrentEnd(isoToDatetimeLocalValue(nextLatest.current_end_at));
        }
        setPendingWindowChange(false);
      }
      await refresh();
    } catch (err) {
      setMessageType("error");
      setMessage(err.message);
    } finally {
      setChecking(false);
    }
  }

  async function deleteCheck(checkId) {
    if (!selectedRegion?.id) return;
    await api.deleteDriftCheck(selectedRegion.id, checkId);
    await refresh();
  }

  async function openRetrainModal() {
    if (!selectedRegion?.id) return;
    setMessage("");
    try {
      const plan = await api.driftRetrainPlan(selectedRegion.id, {
        currentEnd: displayCheck?.current_end_at || currentEnd || undefined,
      });
      setRetrainPlan(plan);
    } catch (err) {
      setMessageType("error");
      setMessage(err.message);
    }
  }

  async function submitRetrain(dataset, selectedModels, modalTrainForm) {
    const payload = {
      ...modalTrainForm,
      cv_splits: Number(modalTrainForm.cv_splits),
      random_state: Number(modalTrainForm.random_state),
      recurrent_sequence_length: Number(modalTrainForm.recurrent_sequence_length || 72),
      recurrent_epochs: Number(modalTrainForm.recurrent_epochs || 3),
      recurrent_batch_size: Number(modalTrainForm.recurrent_batch_size || 32),
      final_test_ratio: Number(modalTrainForm.final_test_ratio || 0.15),
      selected_models: selectedModels,
    };
    const result = await api.trainDataset(dataset.id, payload);
    setRetrainPlan(null);
    setMessageType("success");
    setMessage("Retrain has been queued. Track progress in Airflow or the Datasets tab.");
    await Promise.all([refreshModels(), refreshRegions()]);
    return result;
  }

  function selectCheck(check) {
    setSelectedCheck(check);
    setPendingWindowChange(false);
    if (check.current_end_at) {
      setCurrentEnd(isoToDatetimeLocalValue(check.current_end_at));
    }
  }

  const displayCheck = pendingWindowChange ? null : selectedCheck;
  const featureRows = pendingWindowChange ? [] : driftFeatureRows(displayCheck);
  const summary = displayCheck?.feature_drift_json?.summary || {};
  const visibleChecks = historyExpanded ? checks : checks.slice(0, 5);
  const currentStartPreview = useMemo(() => {
    if (!currentEnd) return "-";
    const date = new Date(currentEnd);
    if (Number.isNaN(date.getTime())) return "-";
    date.setDate(date.getDate() - 7);
    return formatDate(date.toISOString());
  }, [currentEnd]);

  return (
    <div className="grid drift-layout">
      <Card
        title="Feature Drift"
        action={(
          <div className="row-actions">
            <label className="drift-toggle">
              <input
                type="checkbox"
                checked={autoRetrain}
                onChange={(event) => setAutoRetrain(event.target.checked)}
              />
              Auto retrain
            </label>
            <button
              className={checking ? "button-loading" : ""}
              disabled={!selectedRegion?.id || checking}
              onClick={() => runCheck(false)}
            >
              {checking && <span className="button-spinner" />}
              {checking ? "Checking" : "Check now"}
            </button>
          </div>
        )}
      >
        {message && <div className={`alert alert-${messageType}`}>{message}</div>}
        {loading && <div className="alert alert-info">Loading drift checks...</div>}
        {pendingWindowChange && !checking && (
          <div className="alert alert-info">
            Selected current window has not been checked yet. Click Check now to calculate new feature scores.
          </div>
        )}
        <div className="drift-summary">
          <div className={`drift-status-card drift-status-${pendingWindowChange ? "pending" : displayCheck?.status || "none"}`}>
            <span>Status</span>
            <strong>{pendingWindowChange ? "Not checked" : driftStatusText(displayCheck)}</strong>
          </div>
          <div>
            <span>Drifted features</span>
            <strong>{displayCheck ? `${displayCheck.drifted_feature_count}/${displayCheck.feature_count}` : "-"}</strong>
          </div>
          <div>
            <span>Reference window</span>
            <strong>{displayCheck ? `${formatDate(displayCheck.reference_start_at)} - ${formatDate(displayCheck.reference_end_at)}` : "-"}</strong>
          </div>
          <div>
            <span>Current window</span>
            <strong>{currentEnd ? `${currentStartPreview} -` : "-"}</strong>
            <input
              className="drift-window-input"
              type="datetime-local"
              min={isoToDatetimeLocalValue(productionWindow?.production_start_at)}
              max={isoToDatetimeLocalValue(productionWindow?.production_end_at)}
              value={currentEnd}
              onChange={(event) => {
                setCurrentEnd(event.target.value);
                setPendingWindowChange(true);
              }}
            />
          </div>
        </div>
        {displayCheck?.error_message && <div className="alert alert-error">{displayCheck.error_message}</div>}
        {displayCheck?.triggered_training_run_id && (
          <div className="alert alert-success">
            Retrain triggered: {displayCheck.triggered_training_run_id}
          </div>
        )}
        {displayCheck?.feature_drift_json?.retrain_skip_reason && (
          <div className="alert alert-info">
            Retrain skipped: {displayCheck.feature_drift_json.retrain_skip_reason}
          </div>
        )}
        {displayCheck?.drift_detected && !displayCheck?.triggered_training_run_id && (
          <div className="drift-retrain-action">
            <span>Drift is detected for this region.</span>
            <button disabled={checking} onClick={openRetrainModal}>
              Retrain now
            </button>
          </div>
        )}
      </Card>

      <Card title="Feature Scores">
        {featureRows.length ? (
          <Table
            columns={["Feature", "Type", "Metric", "Score", "Threshold", "Status"]}
            rows={featureRows.map((feature) => [
              feature.name,
              feature.type,
              feature.metric,
              feature.score.toFixed(4),
              feature.threshold.toFixed(4),
              <StatusBadge value={feature.drifted ? "drift_detected" : "stable"} />,
            ])}
          />
        ) : (
          <p className="muted">
            {pendingWindowChange
              ? "Selected current window has not been checked yet."
              : "No feature-level scores yet. Run a drift check for the selected region."}
          </p>
        )}
        {displayCheck && !pendingWindowChange && (
          <p className="muted drift-threshold-note">
            Drift is detected when at least {summary.min_drifted_features ?? "-"} features exceed their thresholds.
          </p>
        )}
      </Card>

      <Card
        title="Drift History"
        action={checks.length > 5 && (
          <button className="secondary compact-button" type="button" onClick={() => setHistoryExpanded((value) => !value)}>
            {historyExpanded ? "Show less" : "Show all"}
          </button>
        )}
      >
        {visibleChecks.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Created</th>
                  <th>Status</th>
                  <th>Drifted</th>
                  <th>Retrain</th>
                  <th>Method</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {visibleChecks.map((check) => (
                  <tr
                    className={selectedCheck?.id === check.id ? "selectable-row active" : "selectable-row"}
                    key={check.id}
                    onClick={() => selectCheck(check)}
                  >
                    <td>{formatDate(check.created_at)}</td>
                    <td><StatusBadge value={check.status} /></td>
                    <td>{`${check.drifted_feature_count}/${check.feature_count}`}</td>
                    <td>{check.triggered_training_run_id ? "yes" : "-"}</td>
                    <td>{check.method || "auto"}</td>
                    <td>
                      <button
                        className="icon-button danger-icon"
                        title="Delete drift check"
                        type="button"
                        onClick={(event) => {
                          event.stopPropagation();
                          deleteCheck(check.id).catch((err) => {
                            setMessageType("error");
                            setMessage(err.message);
                          });
                        }}
                      >
                        <Icon name="trash" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted">No drift checks yet.</p>
        )}
      </Card>
      {retrainPlan && (
        <TrainModelModal
          dataset={retrainPlan.dataset}
          trainForm={retrainPlan.configuration}
          initialSelectedModels={retrainPlan.selected_models || defaultTrainingModels}
          eyebrow="Retraining"
          title="Select models"
          submitVerb="Retrain"
          onClose={() => setRetrainPlan(null)}
          onSubmit={submitRetrain}
        />
      )}
    </div>
  );
}

function UserManager() {
  const [users, setUsers] = useState([]);
  const [message, setMessage] = useState("");

  async function refresh() {
    const data = await api.users();
    setUsers(data.items || []);
  }

  useEffect(() => {
    refresh().catch((err) => setMessage(err.message));
  }, []);

  async function update(user, patch) {
    await api.updateUser(user.id, patch);
    await refresh();
  }

  return (
    <Card title="User Management">
      {message && <div className="error">{message}</div>}
      <Table
        columns={["Email", "Role", "Active", "Last Login", "Actions"]}
        rows={users.map((user) => [
          user.email,
          <StatusBadge value={user.role} />,
          user.is_active ? "yes" : "no",
          formatDate(user.last_login_at),
          <div className="row-actions">
            <button onClick={() => update(user, { role: user.role === "admin" ? "user" : "admin" })}>Toggle role</button>
            <button onClick={() => update(user, { is_active: !user.is_active })}>{user.is_active ? "Disable" : "Enable"}</button>
          </div>,
        ])}
      />
    </Card>
  );
}

function Table({ columns, rows }) {
  if (!rows.length) return <p className="muted">No data yet.</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Icon({ name }) {
  const common = {
    width: 24,
    height: 24,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 2,
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": "true",
  };
  const paths = {
    dashboard: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" />
        <rect x="14" y="14" width="7" height="7" rx="1" />
      </>
    ),
    predict: (
      <>
        <path d="M4 19V5" />
        <path d="M4 19h16" />
        <path d="m7 15 3-3 3 2 5-7" />
      </>
    ),
    regions: (
      <>
        <path d="M12 21s7-5.1 7-11a7 7 0 1 0-14 0c0 5.9 7 11 7 11Z" />
        <circle cx="12" cy="10" r="2" />
      </>
    ),
    datasets: (
      <>
        <path d="M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7Z" />
        <path d="M14 2v5h5" />
        <path d="M9 13h6" />
        <path d="M9 17h6" />
      </>
    ),
    models: (
      <>
        <path d="M12 3v18" />
        <path d="M5 8h14" />
        <path d="M5 16h14" />
        <circle cx="5" cy="8" r="2" />
        <circle cx="19" cy="16" r="2" />
      </>
    ),
    drift: (
      <>
        <path d="M4 19V5" />
        <path d="M4 19h16" />
        <path d="M7 15c2.5-6 4.5-6 7 0s4.5 6 7 0" />
        <path d="M8 8h.01" />
        <path d="M16 8h.01" />
      </>
    ),
    "drift-retrain": (
      <>
        <path d="M20 7v5h-5" />
        <path d="M20 12a8 8 0 1 1-2.34-5.66" />
        <path d="M12 8v4l3 2" />
      </>
    ),
    users: (
      <>
        <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
        <circle cx="9" cy="7" r="4" />
        <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
        <path d="M16 3.13a4 4 0 0 1 0 7.75" />
      </>
    ),
    user: (
      <>
        <path d="M20 21a8 8 0 0 0-16 0" />
        <circle cx="12" cy="8" r="4" />
      </>
    ),
    logout: (
      <>
        <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
        <path d="m16 17 5-5-5-5" />
        <path d="M21 12H9" />
      </>
    ),
    chevron: <path d="m15 18-6-6 6-6" />,
    trash: (
      <>
        <path d="M3 6h18" />
        <path d="M8 6V4h8v2" />
        <path d="M19 6l-1 14H6L5 6" />
        <path d="M10 11v6" />
        <path d="M14 11v6" />
      </>
    ),
    lock: (
      <>
        <rect x="4" y="11" width="16" height="10" rx="2" />
        <path d="M8 11V7a4 4 0 0 1 8 0v4" />
      </>
    ),
    trash: (
      <>
        <path d="M3 6h18" />
        <path d="M8 6V4h8v2" />
        <path d="M19 6l-1 14H6L5 6" />
        <path d="M10 11v5" />
        <path d="M14 11v5" />
      </>
    ),
  };
  return <svg {...common}>{paths[name]}</svg>;
}

function Sidebar({
  user,
  page,
  pages: visiblePages,
  collapsed,
  onToggle,
  onNavigate,
  onLogout,
  onProfile,
}) {
  return (
    <aside className={`sidebar ${collapsed ? "sidebar-collapsed" : ""}`}>
      <div className="sidebar-header">
        {!collapsed && <h1>Traffic Panel</h1>}
        <button className="sidebar-toggle" type="button" onClick={onToggle} aria-label="Toggle sidebar">
          <Icon name="chevron" />
        </button>
      </div>

      <button className={`profile-card ${page === "profile" ? "active" : ""}`} type="button" onClick={onProfile}>
        <span className="profile-avatar"><Icon name="user" /></span>
        {!collapsed && (
          <span className="profile-text">
            <strong>{user.display_name || user.email.split("@")[0]}</strong>
            <small>{user.email}</small>
          </span>
        )}
      </button>

      <nav className="sidebar-nav">
        {visiblePages.map(([id, label]) => (
          <button className={page === id ? "active" : ""} key={id} onClick={() => onNavigate(id)} title={label}>
            <Icon name={id} />
            {!collapsed && <span>{label}</span>}
          </button>
        ))}
      </nav>

      <div className="sidebar-footer">
        <button className="logout-button" type="button" onClick={onLogout} title="Logout">
          <Icon name="logout" />
          {!collapsed && <span>Logout</span>}
        </button>
      </div>
    </aside>
  );
}

function ProfilePage({ user, onChangePassword, onProfileUpdated }) {
  const [displayName, setDisplayName] = useState(user.display_name || "");
  const [message, setMessage] = useState("");
  const [messageType, setMessageType] = useState("info");
  const [saving, setSaving] = useState(false);

  async function saveProfile(event) {
    event.preventDefault();
    setMessage("");
    setSaving(true);
    try {
      const updated = await api.updateMe({ display_name: displayName });
      onProfileUpdated(updated);
      setMessageType("success");
      setMessage("Profile updated.");
    } catch (err) {
      setMessageType("error");
      setMessage(err.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="profile-layout">
      <Card title="User Profile">
        <div className="profile-page">
          <div className="profile-hero">
            <span className="profile-page-avatar"><Icon name="user" /></span>
            <div>
              <h3>{user.display_name || user.email.split("@")[0]}</h3>
              <p className="muted">{user.email}</p>
            </div>
          </div>
          <form className="profile-form" onSubmit={saveProfile}>
            <label>
              Display name
              <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="Your name" />
            </label>
            <button disabled={saving}>{saving ? "Saving..." : "Save profile"}</button>
          </form>
          {message && <div className={`alert alert-${messageType}`}>{message}</div>}
          <div className="profile-details">
            <div>
              <span>Role</span>
              <strong>{user.role}</strong>
            </div>
            <div>
              <span>Status</span>
              <strong>{user.is_active ? "Active" : "Inactive"}</strong>
            </div>
            <div>
              <span>Last login</span>
              <strong>{formatDate(user.last_login_at)}</strong>
            </div>
            <div>
              <span>Created</span>
              <strong>{formatDate(user.created_at)}</strong>
            </div>
          </div>
          <button onClick={onChangePassword}>Change password</button>
        </div>
      </Card>
    </div>
  );
}

function ChangePasswordModal({ onClose }) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [message, setMessage] = useState("");
  const [messageType, setMessageType] = useState("info");
  const [loading, setLoading] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setMessage("");
    if (newPassword !== confirmPassword) {
      setMessageType("error");
      setMessage("New passwords do not match.");
      return;
    }
    setLoading(true);
    try {
      await api.changePassword({
        current_password: currentPassword,
        new_password: newPassword,
      });
      setMessageType("success");
      setMessage("Password updated.");
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (err) {
      setMessageType("error");
      setMessage(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <form className="modal-card" onSubmit={submit} onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <div>
            <p className="eyebrow">Profile</p>
            <h2>Change password</h2>
          </div>
          <button className="secondary" type="button" onClick={onClose}>Close</button>
        </div>
        <label>
          Current password
          <input type="password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} />
        </label>
        <label>
          New password
          <input type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} />
        </label>
        <label>
          Confirm new password
          <input type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} />
        </label>
        {message && <div className={`alert alert-${messageType}`}>{message}</div>}
        <button disabled={loading || !currentPassword || !newPassword || !confirmPassword}>
          {loading ? "Updating..." : "Update password"}
        </button>
      </form>
    </div>
  );
}

function App() {
  const [user, setUser] = useState(null);
  const [page, setPage] = useState("dashboard");
  const [regions, setRegions] = useState([]);
  const [selectedRegionId, setSelectedRegionId] = useState("");
  const [datasets, setDatasets] = useState([]);
  const [models, setModels] = useState([]);
  const [error, setError] = useState("");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [showPasswordModal, setShowPasswordModal] = useState(false);

  const selectedRegion = useMemo(
    () => regions.find((region) => region.id === selectedRegionId) || regions[0],
    [regions, selectedRegionId],
  );

  async function loadMe() {
    const current = await api.me();
    setUser(current);
    return current;
  }

  async function refreshRegions(currentUser = user) {
    const data = currentUser?.role === "admin" ? await api.adminRegions() : await api.publicRegions();
    const items = data.items || [];
    setRegions(items);
    if (!items.length) {
      setSelectedRegionId("");
      setDatasets([]);
      setModels([]);
      return;
    }
    setSelectedRegionId((currentId) => (
      currentId && items.some((region) => region.id === currentId)
        ? currentId
        : items[0].id
    ));
  }

  async function refreshDatasets() {
    if (!selectedRegion || user?.role !== "admin") {
      setDatasets([]);
      return;
    }
    const data = await api.datasets(selectedRegion.id);
    setDatasets(data.items || []);
  }

  async function refreshModels() {
    if (!selectedRegion || user?.role !== "admin") {
      setModels([]);
      return;
    }
    const data = await api.modelVersions(selectedRegion.id);
    setModels(data.items || []);
  }

  async function bootstrap() {
    setError("");
    try {
      const current = await loadMe();
      await refreshRegions(current);
    } catch (err) {
      setStoredToken("");
      setUser(null);
      setError(err.message);
    }
  }

  useEffect(() => {
    if (getStoredToken()) bootstrap();
  }, []);

  useEffect(() => {
    if (user && selectedRegion) {
      refreshDatasets().catch(() => setDatasets([]));
      refreshModels().catch(() => setModels([]));
    }
  }, [user?.id, selectedRegion?.id]);

  if (!user) return <AuthPage onLogin={bootstrap} initialError={error} />;

  const visiblePages = pages.filter(([id]) => user.role === "admin" || ["dashboard", "predict"].includes(id));

  return (
    <div className={`app-shell ${sidebarCollapsed ? "app-shell-collapsed" : ""}`}>
      <Sidebar
        user={user}
        page={page}
        pages={visiblePages}
        collapsed={sidebarCollapsed}
        onToggle={() => setSidebarCollapsed((value) => !value)}
        onNavigate={setPage}
        onProfile={() => setPage("profile")}
        onLogout={() => { setStoredToken(""); setUser(null); }}
      />
      <main>
        <header className="topbar">
          <div>
            <h1>{page === "profile" ? "Profile" : pages.find(([id]) => id === page)?.[1]}</h1>
          </div>
          {page !== "profile" && (
            <RegionDropdown
              regions={regions}
              selectedRegion={selectedRegion}
              onChange={setSelectedRegionId}
            />
          )}
        </header>
        <section className="page-panel" hidden={page !== "dashboard"}>
          <Dashboard regions={regions} selectedRegion={selectedRegion} datasets={datasets} models={models} user={user} />
        </section>
        <section className="page-panel" hidden={page !== "predict"}>
          <PredictionPage selectedRegion={selectedRegion} />
        </section>
        {user.role === "admin" && (
          <>
            <section className="page-panel" hidden={page !== "regions"}>
              <RegionManager
                regions={regions}
                selectedRegion={selectedRegion}
                refresh={() => refreshRegions(user)}
              />
            </section>
            <section className="page-panel" hidden={page !== "datasets"}>
              <DatasetManager
                selectedRegion={selectedRegion}
                datasets={datasets}
                refreshDatasets={refreshDatasets}
                refreshModels={refreshModels}
                refreshRegions={() => refreshRegions(user)}
              />
            </section>
            <section className="page-panel" hidden={page !== "models"}>
              <ModelManager selectedRegion={selectedRegion} models={models} refreshModels={refreshModels} refreshRegions={() => refreshRegions(user)} />
            </section>
            <section className="page-panel" hidden={page !== "drift"}>
              <DriftMonitor
                selectedRegion={selectedRegion}
                refreshModels={refreshModels}
                refreshRegions={() => refreshRegions(user)}
              />
            </section>
            <section className="page-panel" hidden={page !== "users"}>
              <UserManager />
            </section>
          </>
        )}
        <section className="page-panel" hidden={page !== "profile"}>
          <ProfilePage
            user={user}
            onProfileUpdated={setUser}
            onChangePassword={() => setShowPasswordModal(true)}
          />
        </section>
      </main>
      {showPasswordModal && <ChangePasswordModal onClose={() => setShowPasswordModal(false)} />}
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
