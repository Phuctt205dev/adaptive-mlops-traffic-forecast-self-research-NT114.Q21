export const pages = [
  ["dashboard", "Dashboard"], ["predict", "Prediction"], ["regions", "Regions"],
  ["datasets", "Datasets"], ["models", "Models"], ["training", "Training Monitor"],
  ["quality", "Model Quality"], ["importance", "Feature Importance"], ["drift", "Drift"], ["users", "Users"],
];

export function pageForUser(page, user) {
  const allowed = user?.role === "admin" ? pages.map(([id]) => id).concat("profile") : ["dashboard", "predict", "profile"];
  return allowed.includes(page) ? page : "dashboard";
}

export function rememberedPage(user, storage) {
  if (!user?.id) return "dashboard";
  try { return pageForUser((storage || globalThis.localStorage).getItem(`traffic_page_${user.id}`), user); }
  catch { return "dashboard"; }
}

export function rememberPage(user, page, storage) {
  const next = pageForUser(page, user);
  if (user?.id) {
    try { (storage || globalThis.localStorage).setItem(`traffic_page_${user.id}`, next); }
    catch { /* Navigation remains usable when storage is blocked. */ }
  }
  return next;
}
