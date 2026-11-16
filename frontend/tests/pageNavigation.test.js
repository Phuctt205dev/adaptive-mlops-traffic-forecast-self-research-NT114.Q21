import test from "node:test";
import assert from "node:assert/strict";
import { pages, pageForUser, rememberedPage, rememberPage } from "../src/pageNavigation.js";

function storage() {
  const values = new Map();
  return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
}

test("every admin tab and profile survives a reload lookup", () => {
  const store = storage();
  const admin = { id: "admin-a", role: "admin" };
  for (const page of pages.map(([id]) => id).concat("profile")) {
    assert.equal(rememberPage(admin, page, store), page);
    assert.equal(rememberedPage(admin, store), page);
  }
});

test("navigation is scoped to the account and validates current permissions", () => {
  const store = storage();
  const admin = { id: "admin-a", role: "admin" };
  rememberPage(admin, "models", store);
  assert.equal(rememberedPage({ id: "admin-b", role: "admin" }, store), "dashboard");
  assert.equal(rememberedPage({ ...admin, role: "user" }, store), "dashboard");
  for (const page of ["dashboard", "predict", "profile"]) {
    assert.equal(pageForUser(page, { role: "user" }), page);
  }
  for (const page of [null, "removed-page", "__proto__", "users"]) {
    assert.equal(pageForUser(page, { role: "user" }), "dashboard");
  }
});

test("blocked storage does not break navigation or authentication", () => {
  const blocked = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  const admin = { id: "admin", role: "admin" };
  assert.equal(rememberedPage(admin, blocked), "dashboard");
  assert.equal(rememberPage(admin, "datasets", blocked), "datasets");
  assert.equal(rememberedPage(null, blocked), "dashboard");
});
