export const cleanupPhases = {
  preparing: "Checking ownership and waiting for services",
  local_files: "Clearing saved files and model caches",
  mlflow: "Removing training tracking files",
  airflow: "Removing this region's training history",
  objects: "Deleting stored files",
  verifying: "Checking that deletion is complete",
  completed: "Deletion complete",
};

export function pendingCleanupKey(userId, kind) {
  return `traffic_cleanup:${userId}:${kind}`;
}

export function readPendingCleanup(userId, kind) {
  try {
    const value = JSON.parse(sessionStorage.getItem(pendingCleanupKey(userId, kind)) || "null");
    return value && typeof value.id === "string" && typeof value.subject === "string" ? value : null;
  } catch { return null; }
}

export function cleanupView(status) {
  if (status?.state === "completed") return "completed";
  if (status?.state === "retry_required") return "retry";
  return "checking";
}
