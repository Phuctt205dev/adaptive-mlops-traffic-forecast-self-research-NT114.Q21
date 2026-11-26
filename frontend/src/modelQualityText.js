// Text for the Model Quality tab. A raw vehicle difference alone looks small
// or large depending on how busy the hour is, so errors are also shown as a
// share of actual traffic and against a naive "same hour last week" guess.

const finite = (value) => typeof value === "number" && Number.isFinite(value);

export const numberText = (value) => value == null ? "—" : Number(value).toLocaleString("en", { maximumFractionDigits: 2 });

export function percentText(value) {
  if (!finite(value)) return "—";
  return `${value.toLocaleString("en", { maximumFractionDigits: value >= 100 ? 0 : 1 })}%`;
}

// Older backends only send MAE/RMSE; the panel then keeps the vehicle view.
export const hasErrorShare = (metrics) => finite(metrics?.wape_percent);

// Below this the model and last week's guess count as equally good.
const SAME_WITHIN_PERCENT = 0.5;

export function baselineComparison(metrics) {
  const baseline = metrics?.baseline;
  if (baseline?.method !== "same_hour_last_week" || !finite(baseline.relative_change_percent)
      || !(baseline.paired_points > 0)) return null;
  const change = baseline.relative_change_percent;
  if (Math.abs(change) < SAME_WITHIN_PERCENT) {
    return { text: "About the same", worse: false, baselineMae: baseline.mae, hours: baseline.paired_points };
  }
  return {
    text: `${percentText(Math.abs(change))} ${change < 0 ? "better" : "worse"}`,
    worse: change > 0, baselineMae: baseline.mae, hours: baseline.paired_points,
  };
}

export function differenceText(point) {
  if (point?.absolute_error == null) return "—";
  const vehicles = numberText(point.absolute_error);
  return finite(point.percent_error) ? `${vehicles} (${percentText(point.percent_error)})` : vehicles;
}
