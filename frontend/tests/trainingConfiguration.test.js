import test from "node:test";
import assert from "node:assert/strict";
import { normalizeTrainingConfiguration } from "../src/trainingConfiguration.js";

const form = {
  train_start_date: "2026-02-01", train_end_date: "2026-08-01",
  cv_splits: "4", random_state: "0", recurrent_sequence_length: "48",
  recurrent_epochs: "7", recurrent_batch_size: "64", final_test_ratio: "0.25",
  artifact_root: "models/regions", model_role: "candidate", training_backend: "kaggle",
};

test("nondefault settings and dates survive conversion, including random seed zero", () => {
  const result = normalizeTrainingConfiguration(form);
  assert.equal(result.train_start_date, "2026-02-01");
  assert.equal(result.train_end_date, "2026-08-01");
  assert.equal(result.random_state, 0);
  assert.equal(result.cv_splits, 4);
  assert.equal(result.final_test_ratio, 0.25);
  assert.equal(result.recurrent_epochs, 7);
  assert.equal(result.training_backend, "kaggle");
  assert.equal(form.cv_splits, "4");
});

test("invalid settings fail instead of silently falling back to defaults", () => {
  const invalid = {
    train_start_date: ["", "2026-02-30", "2026-08-01", "2026-09-01"],
    train_end_date: ["", "2026-01-01"], cv_splits: ["", "1", "2.5", "abc"],
    random_state: ["-1", "4294967296"], recurrent_sequence_length: ["0", ""],
    recurrent_epochs: ["0", "-1"], recurrent_batch_size: ["0", "1.5"],
    final_test_ratio: ["", "0", "0.5", "NaN", "Infinity"],
  };
  for (const [key, values] of Object.entries(invalid)) {
    for (const value of values) assert.throws(() => normalizeTrainingConfiguration({ ...form, [key]: value }), `${key}=${value}`);
  }
});
