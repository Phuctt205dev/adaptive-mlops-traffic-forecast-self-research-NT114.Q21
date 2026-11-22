// Helpers that keep asynchronous data tied to the region the admin is looking at.
// Pure functions so the rules can be tested without a browser.

const EMPTY = Object.freeze([]);

// A gate lets only the most recently started request update the screen.
export function createRequestGate() {
  let latest = 0;
  return {
    begin() {
      latest += 1;
      return latest;
    },
    isCurrent(token) {
      return token === latest;
    },
    invalidate() {
      latest += 1;
    },
  };
}

// Rows loaded for one region are never shown under another one.
export function scopedItems(scope, regionId) {
  if (!scope || !regionId || scope.regionId !== regionId) return EMPTY;
  return scope.items || EMPTY;
}

export function isScopeLoaded(scope, regionId) {
  return Boolean(regionId) && scope?.regionId === regionId;
}

// Rows without a region id (older payloads) are accepted; a different id is not.
export function belongsToRegion(item, regionId) {
  if (!item || !regionId) return false;
  return item.region_id == null || item.region_id === regionId;
}

export function itemsForRegion(items, regionId) {
  return (items || []).filter((item) => belongsToRegion(item, regionId));
}

// Number of whole hours since the epoch; changes exactly when a new hour starts.
export function hourBucket(now = Date.now()) {
  return Math.floor(now / 3600000);
}
