import test from "node:test";
import assert from "node:assert/strict";
import {
  decisionNote, driftStatusClass, driftStatusText, historyRetrainText, historyTrafficText,
  retrainReasonText, scheduleText, skipReasonText, trafficChange,
} from "../src/driftText.js";

const check = (summary = {}, extra = {}, status = "stable") => ({
  status, triggered_training_run_id: null,
  feature_drift_json: { summary: { decision_signal: "traffic_volume_psi", traffic_psi: 0.3456,
    traffic_psi_threshold: 1, drift_detected: false, scheduled_retrain_days: 28, model_data_age_days: 12.6, ...summary },
  ...extra },
});

test("traffic change shows PSI against its threshold", () => {
  assert.deepEqual(trafficChange(check()), { text: "0.35 / 1.00", drifted: false });
  assert.deepEqual(trafficChange(check({ traffic_psi: 2.5, drift_detected: true })), { text: "2.50 / 1.00", drifted: true });
  assert.equal(trafficChange(check({ traffic_psi: null })).text, "-");
});

test("checks saved before the change have no traffic card", () => {
  const old = { status: "drift_detected", feature_drift_json: { summary: { drift_detected: true, min_drifted_features: 2 } } };
  assert.equal(trafficChange(old), null);
  assert.equal(scheduleText(old), null);
  assert.equal(decisionNote(old), "Drift is detected when at least 2 features exceed their thresholds.");
  assert.equal(driftStatusText(old), "Drift detected");
  assert.equal(historyTrafficText(old), "-");
});

test("decision note says weather scores are reference only", () => {
  assert.match(decisionNote(check()), /PSI of traffic_volume above 1\.00/);
  assert.match(decisionNote(check()), /reference only/);
});

test("a scheduled retrain is not shown as a drift warning", () => {
  const scheduled = check({}, { retrain_reason: "scheduled" }, "retrain_triggered");
  scheduled.triggered_training_run_id = "run-1";
  assert.equal(driftStatusText(scheduled), "Scheduled retrain");
  assert.equal(driftStatusClass(scheduled), "scheduled");
  assert.equal(retrainReasonText(scheduled), "scheduled, model data older than 28 days");
  assert.equal(historyRetrainText(scheduled), "scheduled");
});

test("a traffic retrain keeps the warning status", () => {
  const traffic = check({ drift_detected: true }, { retrain_reason: "traffic_drift" }, "retrain_triggered");
  traffic.triggered_training_run_id = "run-2";
  assert.equal(driftStatusText(traffic), "Retrain triggered");
  assert.equal(driftStatusClass(traffic), "retrain_triggered");
  assert.equal(retrainReasonText(traffic), "traffic changed");
  assert.equal(historyRetrainText(traffic), "traffic");
});

test("schedule text shows model data age or that the schedule is off", () => {
  assert.equal(scheduleText(check()), "Model data is 12 days old at this check; scheduled retrain at 28 days.");
  assert.equal(scheduleText(check({ scheduled_retrain_days: 0 })), "Scheduled retrain is off.");
});

test("skip reasons are explained in words", () => {
  assert.match(skipReasonText("candidate_pending"), /waiting to be activated/);
  assert.match(skipReasonText("cooldown_active"), /cooldown/);
  assert.equal(skipReasonText("something_new"), "something_new");
});

test("history shows PSI with two decimals and a dash for old checks", () => {
  assert.equal(historyTrafficText(check()), "0.35");
  assert.equal(historyRetrainText(check()), "-");
});
