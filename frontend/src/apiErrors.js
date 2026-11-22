// Turns an API error body into one readable sentence.
// FastAPI validation errors (422) arrive as a list of objects, which used to
// be shown as "[object Object]".

function fieldLabel(location) {
  if (!Array.isArray(location)) return "";
  const names = location.filter((part) => typeof part === "string" && !["body", "query", "path"].includes(part));
  const last = names[names.length - 1];
  if (!last) return "";
  const text = last.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function validationText(item) {
  if (typeof item === "string") return item;
  const raw = typeof item?.msg === "string" ? item.msg : "";
  if (!raw) return "";
  const message = raw.replace(/^Value error,\s*/i, "");
  const field = fieldLabel(item.loc);
  return field ? `${field}: ${message}` : message;
}

export function apiErrorMessage(payload, status) {
  const direct = payload?.error?.message;
  if (typeof direct === "string" && direct) return direct;
  const detail = payload?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map(validationText).filter(Boolean);
    if (parts.length) return [...new Set(parts)].join("; ");
  }
  if (typeof detail?.message === "string" && detail.message) return detail.message;
  if (status >= 500) return `The server could not complete the request (HTTP ${status}). Please try again.`;
  return `HTTP ${status}`;
}
