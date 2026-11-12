/** fetch helpers. Paths are /api/... — Vite proxies them to uvicorn. */

export async function apiGet(path) {
  const res = await fetch(path);
  return readJson(res);
}

export async function apiPost(path, body, timeoutMs = 120000) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  });
  return readJson(res);
}

export async function apiForm(path, formData) {
  const res = await fetch(path, { method: "POST", body: formData });
  return readJson(res);
}

/** File download. Returns { blob, filename, pointCount, seriesCount, errors }. */
export async function apiDownload(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(10 * 60 * 1000),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch {
      /* body was not JSON */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  const blob = await res.blob();
  const disposition = res.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/);
  const errorsRaw = res.headers.get("X-Partial-Errors") || "";
  return {
    blob,
    filename: match ? match[1] : "download",
    pointCount: Number(res.headers.get("X-Point-Count") || 0),
    seriesCount: Number(res.headers.get("X-Series-Count") || 0),
    errors: errorsRaw ? decodeURIComponent(errorsRaw) : "",
  };
}

// Text X-Dem-* values are percent-encoded, same as X-Partial-Errors.
const DEM_HEADERS = [
  ["provider", "X-Dem-Provider"],
  ["requestedResolution", "X-Dem-Requested-Resolution"],
  ["sourceResolution", "X-Dem-Source-Resolution"],
  ["coarsestResolution", "X-Dem-Coarsest-Resolution"],
  ["epsg", "X-Dem-Epsg"],
  ["bboxProjected", "X-Dem-Bbox-Projected"],
  ["verticalUnit", "X-Dem-Vertical-Unit"],
  ["fromCache", "X-Dem-From-Cache"],
  ["collectionKey", "X-Dem-Collection-Key"],
  ["collectionName", "X-Dem-Collection-Name"],
  ["collectionPeriod", "X-Dem-Collection-Period"],
  ["fillKey", "X-Dem-Fill-Key"],
  ["fillName", "X-Dem-Fill-Name"],
  ["fillPeriod", "X-Dem-Fill-Period"],
  ["fillUsed", "X-Dem-Fill-Used"],
  ["nodata", "X-Dem-Nodata"],
  ["clipName", "X-Dem-Clip-Name"],
  ["description", "X-Dem-Description"],
  ["zeroWarning", "X-Dem-Zero-Warning"],
];

function headerText(res, name) {
  const raw = res.headers.get(name);
  if (!raw) return "";
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

/** GeoTIFF download. Returns { blob, filename, dem }. dem is the decoded X-Dem-* headers. */
export async function apiDemDownload(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(10 * 60 * 1000),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch {
      /* body was not JSON */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  const blob = await res.blob();
  const disposition = res.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/);
  const dem = {};
  for (const [key, name] of DEM_HEADERS) {
    dem[key] = headerText(res, name);
  }
  return {
    blob,
    filename: match ? match[1] : "dem.tif",
    dem,
  };
}

async function readJson(res) {
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const detail = (data && data.detail) || res.statusText || "Request failed";
    const message = typeof detail === "string" ? detail : JSON.stringify(detail);
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  return data;
}

export function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  // Firefox drops the download if the link isn't in the page,
  // or if the object URL is released in the same turn as the click.
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function saveJson(obj, filename) {
  const blob = new Blob([JSON.stringify(obj)], { type: "application/geo+json" });
  saveBlob(blob, filename);
}
