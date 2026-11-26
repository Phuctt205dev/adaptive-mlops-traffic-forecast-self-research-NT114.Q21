import test from "node:test";
import assert from "node:assert/strict";
import {
  baselineComparison, differenceText, hasErrorShare, percentText,
} from "../src/modelQualityText.js";

const baseline = (change, extra = {}) => ({
  baseline: { method: "same_hour_last_week", lag_hours: 168, paired_points: 72, mae: 410,
              model_mae: 340, relative_change_percent: change, ...extra },
});

test("percent keeps one decimal below 100% and hides missing values", () => {
  assert.equal(percentText(6.84), "6.8%");
  assert.equal(percentText(12), "12%");
  assert.equal(percentText(1000), "1,000%");
  assert.equal(percentText(null), "—");
  assert.equal(percentText(Number.NaN), "—");
});

test("an older backend without WAPE keeps the vehicle-only view", () => {
  assert.equal(hasErrorShare({ mae: 250, rmse: 290 }), false);
  assert.equal(hasErrorShare({ mae: 250, wape_percent: null }), false);
  assert.equal(hasErrorShare({ mae: 250, wape_percent: 10 }), true);
  assert.equal(hasErrorShare(undefined), false);
});

test("a smaller model error than last week reads as better", () => {
  const result = baselineComparison(baseline(-41.18));
  assert.equal(result.text, "41.2% better");
  assert.equal(result.worse, false);
  assert.equal(result.hours, 72);
});

test("a larger model error than last week is a warning", () => {
  const result = baselineComparison(baseline(40));
  assert.equal(result.text, "40% worse");
  assert.equal(result.worse, true);
});

test("tiny differences are reported as about the same", () => {
  assert.equal(baselineComparison(baseline(0.2)).text, "About the same");
  assert.equal(baselineComparison(baseline(-0.4)).worse, false);
});

test("no comparison when last week is missing or the backend is older", () => {
  assert.equal(baselineComparison(baseline(null)), null);
  assert.equal(baselineComparison(baseline(-10, { paired_points: 0 })), null);
  assert.equal(baselineComparison({ mae: 250 }), null);
  assert.equal(baselineComparison(baseline(-10, { method: "other" })), null);
});

test("hourly difference shows vehicles and percent of actual traffic", () => {
  assert.equal(differenceText({ absolute_error: 120, percent_error: 12 }), "120 (12%)");
  assert.equal(differenceText({ absolute_error: 1100, percent_error: null }), "1,100");
  assert.equal(differenceText({ absolute_error: 200 }), "200");
  assert.equal(differenceText({ absolute_error: null, percent_error: null }), "—");
});
