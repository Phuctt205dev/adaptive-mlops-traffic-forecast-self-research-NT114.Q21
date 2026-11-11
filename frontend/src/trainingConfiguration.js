const integerFields = [
  ["cv_splits", "CV folds", 2],
  ["random_state", "Random seed", 0, 4294967295],
  ["recurrent_sequence_length", "Sequence length", 1],
  ["recurrent_epochs", "Epoch limit", 1],
  ["recurrent_batch_size", "Batch size", 1],
];

export function normalizeTrainingConfiguration(form) {
  const result = { ...form };
  for (const key of ["train_start_date", "train_end_date"]) {
    const value = result[key];
    const date = new Date(`${value}T00:00:00Z`);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "") || value.startsWith("0000")
        || !Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== value) {
      throw new Error("Choose valid training start and end dates.");
    }
  }
  if (result.train_start_date >= result.train_end_date) {
    throw new Error("Training start date must be before the end date.");
  }
  for (const [key, label, min, max] of integerFields) {
    const value = Number(form[key]);
    if (form[key] === "" || form[key] == null || !Number.isInteger(value) || value < min || (max != null && value > max)) {
      throw new Error(`${label} must be a whole number ${max != null ? `from ${min} to ${max}` : `of at least ${min}`}.`);
    }
    result[key] = value;
  }
  const ratio = Number(form.final_test_ratio);
  if (!Number.isFinite(ratio) || ratio <= 0 || ratio >= 0.5) {
    throw new Error("Final test ratio must be greater than 0 and less than 0.5.");
  }
  result.final_test_ratio = ratio;
  return result;
}
