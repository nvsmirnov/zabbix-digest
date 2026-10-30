export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request(method, path, body) {
  const options = {
    method,
    credentials: "same-origin",
    headers: { "X-Requested-With": "zabbix-digest" },
  };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (e) {
    throw new ApiError(0, "network", "The server cannot be reached. Check your connection and try again.");
  }
  let data = null;
  try {
    data = await response.json();
  } catch (e) {
    data = null;
  }
  if (!response.ok) {
    let message = (data && data.message) || `Request failed (HTTP ${response.status}).`;
    if (data && Array.isArray(data.detail)) {
      message = "Invalid input: " + data.detail.map((d) => (d.loc || []).slice(1).join(".") + " " + d.msg).join("; ");
    }
    throw new ApiError(response.status, (data && data.error) || "error", message);
  }
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body) => request("POST", path, body === undefined ? {} : body),
  put: (path, body) => request("PUT", path, body),
  del: (path) => request("DELETE", path),
};

// Zabbix severity names by value (0..5).
export const SEVERITIES = ["Not classified", "Information", "Warning", "Average", "High", "Disaster"];
