function defaultApiBaseUrl() {
  if (window.location.port === "30073") {
    return "http://localhost:30080/api/v1";
  }
  return "http://localhost:8000/api/v1";
}

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || defaultApiBaseUrl();

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

export function getStoredToken() {
  return localStorage.getItem("traffic_token") || "";
}

export function setStoredToken(token) {
  if (token) {
    localStorage.setItem("traffic_token", token);
  } else {
    localStorage.removeItem("traffic_token");
  }
}

async function parseResponse(response) {
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const message =
      payload?.error?.message || payload?.detail || `HTTP ${response.status}`;
    throw new ApiError(message, response.status, payload);
  }
  return payload;
}

export async function apiRequest(path, options = {}) {
  const token = getStoredToken();
  const headers = {
    ...(options.body instanceof FormData
      ? {}
      : { "Content-Type": "application/json" }),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(options.headers || {}),
  };
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
  });
  return parseResponse(response);
}

export const api = {
  login: (email, password) =>
    apiRequest("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  register: (email, password) =>
    apiRequest("/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  me: () => apiRequest("/auth/me"),
  updateMe: (payload) =>
    apiRequest("/auth/me", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  changePassword: (payload) =>
    apiRequest("/auth/password", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  users: () => apiRequest("/admin/users?page_size=100"),
  updateUser: (id, payload) =>
    apiRequest(`/admin/users/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  publicRegions: () => apiRequest("/regions?page_size=100"),
  adminRegions: () => apiRequest("/admin/regions?page_size=100&include_inactive=true"),
  createRegion: (payload) =>
    apiRequest("/admin/regions", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  lookupRegion: (query) =>
    apiRequest(`/admin/regions/lookup?q=${encodeURIComponent(query)}`),
  updateRegion: (id, payload) =>
    apiRequest(`/admin/regions/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteRegion: (id) =>
    apiRequest(`/admin/regions/${id}`, {
      method: "DELETE",
    }),
  datasets: (regionId) =>
    apiRequest(`/admin/regions/${regionId}/datasets?page_size=100`),
  uploadDataset: (regionId, file) => {
    const body = new FormData();
    body.append("file", file);
    return apiRequest(`/admin/regions/${regionId}/datasets`, {
      method: "POST",
      body,
    });
  },
  trainDataset: (datasetId, payload) =>
    apiRequest(`/admin/datasets/${datasetId}/training-runs`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  trainingRun: (trainingRunId) =>
    apiRequest(`/admin/training-runs/${trainingRunId}`),
  modelVersions: (regionId) =>
    apiRequest(`/admin/regions/${regionId}/model-versions?page_size=100`),
  driftChecks: (regionId) =>
    apiRequest(`/admin/regions/${regionId}/drift-checks?limit=10`),
  deleteDriftCheck: (regionId, checkId) =>
    apiRequest(`/admin/regions/${regionId}/drift-checks/${checkId}`, {
      method: "DELETE",
    }),
  runDriftCheck: (regionId, payload = {}) => {
    const params = new URLSearchParams({
      auto_retrain: payload.autoRetrain ? "true" : "false",
      force_retrain: payload.forceRetrain ? "true" : "false",
    });
    if (payload.currentEnd) {
      params.set("current_end", payload.currentEnd);
    }
    return apiRequest(`/admin/regions/${regionId}/drift-checks/run?${params.toString()}`, {
      method: "POST",
    });
  },
  activateModel: (modelVersionId) =>
    apiRequest(`/admin/model-versions/${modelVersionId}/activate`, {
      method: "POST",
    }),
  deleteModel: (modelVersionId) =>
    apiRequest(`/admin/model-versions/${modelVersionId}`, {
      method: "DELETE",
    }),
  predict: (regionId, payload) =>
    apiRequest(`/regions/${regionId}/predictions`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  predictionWindow: (regionId) =>
    apiRequest(`/regions/${regionId}/prediction-window`),
  forecastDashboard: (regionId) =>
    apiRequest(`/regions/${regionId}/forecast-dashboard`),
};
