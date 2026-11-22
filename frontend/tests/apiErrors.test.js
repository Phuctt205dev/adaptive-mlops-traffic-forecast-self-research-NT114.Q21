import test from "node:test";
import assert from "node:assert/strict";
import { apiErrorMessage } from "../src/apiErrors.js";

test("application errors keep their own message", () => {
  assert.equal(
    apiErrorMessage({ error: { code: "last_admin_required", message: "At least one active administrator must remain." } }, 409),
    "At least one active administrator must remain.",
  );
});

test("a validation list becomes readable text instead of [object Object]", () => {
  const payload = { detail: [{ type: "string_too_short", loc: ["body", "new_password"], msg: "String should have at least 8 characters" }] };
  const message = apiErrorMessage(payload, 422);
  assert.equal(message, "New password: String should have at least 8 characters");
  assert.ok(!message.includes("[object"));
});

test("several problems are joined, duplicates removed, Value error prefix dropped", () => {
  const payload = {
    detail: [
      { loc: ["body", "train_start_date"], msg: "Value error, Use a date in YYYY-MM-DD format." },
      { loc: ["body", "cv_splits"], msg: "Input should be greater than or equal to 2" },
      { loc: ["body", "cv_splits"], msg: "Input should be greater than or equal to 2" },
    ],
  };
  assert.equal(
    apiErrorMessage(payload, 422),
    "Train start date: Use a date in YYYY-MM-DD format.; Cv splits: Input should be greater than or equal to 2",
  );
});

test("string and object detail forms are supported", () => {
  assert.equal(apiErrorMessage({ detail: "Not authenticated" }, 401), "Not authenticated");
  assert.equal(apiErrorMessage({ detail: { message: "Nope" } }, 400), "Nope");
  assert.equal(apiErrorMessage({ detail: ["plain"] }, 400), "plain");
});

test("an empty or unknown body falls back to the status", () => {
  assert.equal(apiErrorMessage(null, 404), "HTTP 404");
  assert.equal(apiErrorMessage({ detail: [{}] }, 400), "HTTP 400");
  assert.match(apiErrorMessage(null, 502), /server could not complete the request \(HTTP 502\)/);
});
