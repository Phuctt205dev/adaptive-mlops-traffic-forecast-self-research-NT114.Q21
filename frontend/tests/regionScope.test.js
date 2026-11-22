import test from "node:test";
import assert from "node:assert/strict";
import {
  belongsToRegion, createRequestGate, hourBucket, isScopeLoaded, itemsForRegion, scopedItems,
} from "../src/regionScope.js";

test("a late response from the previous region is rejected by the gate", () => {
  const gate = createRequestGate();
  const regionA = gate.begin();
  const regionB = gate.begin();
  assert.equal(gate.isCurrent(regionA), false);
  assert.equal(gate.isCurrent(regionB), true);
});

test("invalidate drops every response that is still on its way", () => {
  const gate = createRequestGate();
  const token = gate.begin();
  gate.invalidate();
  assert.equal(gate.isCurrent(token), false);
});

test("rows loaded for region A are not visible under region B", () => {
  const scope = { regionId: "A", items: [{ id: 1, region_id: "A" }] };
  assert.equal(scopedItems(scope, "A").length, 1);
  assert.deepEqual([...scopedItems(scope, "B")], []);
  assert.deepEqual([...scopedItems(null, "A")], []);
  assert.deepEqual([...scopedItems(scope, "")], []);
  assert.equal(isScopeLoaded(scope, "A"), true);
  assert.equal(isScopeLoaded(scope, "B"), false);
  assert.equal(isScopeLoaded(scope, ""), false);
});

test("an action target must belong to the selected region", () => {
  assert.equal(belongsToRegion({ region_id: "A" }, "A"), true);
  assert.equal(belongsToRegion({ region_id: "A" }, "B"), false);
  assert.equal(belongsToRegion({ id: "legacy-row-without-region" }, "B"), true);
  assert.equal(belongsToRegion(null, "A"), false);
  assert.equal(belongsToRegion({ region_id: "A" }, ""), false);
});

test("a response containing another region's rows is filtered out", () => {
  const rows = [{ id: 1, region_id: "A" }, { id: 2, region_id: "B" }, { id: 3 }];
  assert.deepEqual(itemsForRegion(rows, "A").map((row) => row.id), [1, 3]);
  assert.deepEqual(itemsForRegion(undefined, "A"), []);
});

test("the hour bucket changes exactly at the top of the hour", () => {
  const hour = Date.UTC(2026, 10, 22, 10, 0, 0);
  assert.equal(hourBucket(hour), hourBucket(hour + 59 * 60000 + 59999));
  assert.notEqual(hourBucket(hour), hourBucket(hour + 3600000));
  assert.equal(hourBucket(hour - 1), hourBucket(hour) - 1);
});
