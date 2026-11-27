// Text for the Drift page. Since experiment E3 the retrain decision uses the
// change of traffic itself (PSI of traffic_volume); weather/holiday scores are
// shown for reference only. Older checks without these fields keep the old text.

const finite = (value) => typeof value === "number" && Number.isFinite(value);
const fixed = (value, digits = 2) => Number(value).toLocaleString("en", {
  minimumFractionDigits: digits, maximumFractionDigits: digits,
});

const SKIP_REASONS = {
  candidate_pending: "A model retrained from the active model is waiting to be activated",
  cooldown_active: "Waiting for the cooldown after the last retrain",
  training_already_running: "Another training run is in progress",
};

export function retrainReason(check) {
  const reason = check?.feature_drift_json?.retrain_reason;
  return reason === "scheduled" || reason === "traffic_drift" ? reason : null;
}

export function driftStatusText(check) {
  if (!check) return "Not checked";
  if (check.status === "retrain_triggered") {
    return retrainReason(check) === "scheduled" ? "Scheduled retrain" : "Retrain triggered";
  }
  if (check.status === "drift_detected") return "Drift detected";
  if (check.status === "stable") return "Stable";
  if (check.status === "skipped") return "Skipped";
  if (check.status === "failed") return "Failed";
  return check.status || "Unknown";
}

// A scheduled retrain is routine maintenance, not a warning.
export function driftStatusClass(check) {
  if (check?.status === "retrain_triggered" && retrainReason(check) === "scheduled") return "scheduled";
  return check?.status || "none";
}

export function trafficChange(check) {
  const summary = check?.feature_drift_json?.summary;
  if (!summary || !("traffic_psi" in summary)) return null;
  const value = summary.traffic_psi;
  const threshold = summary.traffic_psi_threshold;
  return {
    text: finite(value) ? `${fixed(value)} / ${finite(threshold) ? fixed(threshold) : "-"}` : "-",
    drifted: Boolean(summary.drift_detected),
  };
}

export function retrainReasonText(check) {
  const reason = retrainReason(check);
  if (reason === "traffic_drift") return "traffic changed";
  if (reason === "scheduled") {
    const days = check?.feature_drift_json?.summary?.scheduled_retrain_days;
    return finite(days) && days > 0 ? `scheduled, model data older than ${days} days` : "scheduled";
  }
  return null;
}

export function skipReasonText(reason) {
  return SKIP_REASONS[reason] || reason;
}

export function decisionNote(check) {
  const summary = check?.feature_drift_json?.summary || {};
  if (summary.decision_signal === "traffic_volume_psi") {
    const threshold = finite(summary.traffic_psi_threshold) ? fixed(summary.traffic_psi_threshold) : "-";
    return `Drift is decided by the traffic change: PSI of traffic_volume above ${threshold}. `
      + "Weather and holiday scores are shown for reference only.";
  }
  return `Drift is detected when at least ${summary.min_drifted_features ?? "-"} features exceed their thresholds.`;
}

export function scheduleText(check) {
  const summary = check?.feature_drift_json?.summary || {};
  if (!("scheduled_retrain_days" in summary)) return null;
  const days = summary.scheduled_retrain_days;
  if (!finite(days) || days <= 0) return "Scheduled retrain is off.";
  const age = finite(summary.model_data_age_days) ? Math.floor(summary.model_data_age_days) : null;
  return age == null
    ? `Scheduled retrain when the model data is ${days} days old.`
    : `Model data is ${age} days old at this check; scheduled retrain at ${days} days.`;
}

export function historyRetrainText(check) {
  if (!check?.triggered_training_run_id) return "-";
  const reason = retrainReason(check);
  return reason === "scheduled" ? "scheduled" : reason === "traffic_drift" ? "traffic" : "yes";
}

export function historyTrafficText(check) {
  const value = check?.feature_drift_json?.summary?.traffic_psi;
  return finite(value) ? fixed(value) : "-";
}
