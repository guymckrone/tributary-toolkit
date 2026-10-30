/**
 * StreamStats page.
 *
 * Map clicks only move the orange marker and the lat/lon boxes.
 * Snap and Delineate are the only calls to Python.
 */

import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { apiGet, apiPost, saveBlob, saveJson } from "./api.js";

const DEFAULT_LAT = 45.3725;
const DEFAULT_LON = -122.5858;

export function mountStreamStats(root) {
  root.innerHTML = `
    <div class="layout">
      <div class="stack">
        <section class="panel">
          <h2>Pour point</h2>
          <p class="hint">Click the map or type a point. A click does not call USGS.</p>
          <div class="row">
            <div>
              <label for="ss-lat">Latitude</label>
              <input id="ss-lat" type="number" step="0.000001" value="${DEFAULT_LAT}" />
            </div>
            <div>
              <label for="ss-lon">Longitude</label>
              <input id="ss-lon" type="number" step="0.000001" value="${DEFAULT_LON}" />
            </div>
          </div>
          <label for="ss-region">Region (state code)</label>
          <input id="ss-region" type="text" value="OR" />
          <label class="check">
            <input id="ss-ssl" type="checkbox" checked />
            Verify SSL
          </label>
          <p class="hint">Turn this off on the work network. It covers snap and delineate. Stream lines always skip this check.</p>
          <div class="actions">
            <button id="ss-snap" type="button">Snap</button>
            <button id="ss-delin" class="primary" type="button">Delineate watershed</button>
          </div>
          <p id="ss-status" class="status"></p>
          <div id="ss-error"></div>
          <div id="ss-result"></div>
        </section>
        <section class="panel">
          <h2>Batch</h2>
          <p class="hint">CSV with lat/latitude and lon/long/longitude. Optional id or name. Cap 25.</p>
          <input id="ss-csv" type="file" accept=".csv,text/csv" />
          <div class="actions">
            <button id="ss-batch" type="button">Delineate all</button>
          </div>
          <div id="ss-batch-note"></div>
          <div id="ss-batch-table" class="scroll"></div>
        </section>
      </div>
      <section class="panel">
        <div class="actions" style="margin-top:0">
          <button id="ss-flow" type="button">Load streams in this view</button>
        </div>
        <p class="hint">Streams are a guide for aiming. Snap still uses SS-Delineate.</p>
        <div id="ss-map" class="map"></div>
      </section>
    </div>
  `;

  const latInput = root.querySelector("#ss-lat");
  const lonInput = root.querySelector("#ss-lon");
  const regionInput = root.querySelector("#ss-region");
  const sslInput = root.querySelector("#ss-ssl");
  const statusEl = root.querySelector("#ss-status");
  const errorEl = root.querySelector("#ss-error");
  const resultEl = root.querySelector("#ss-result");
  const batchNote = root.querySelector("#ss-batch-note");
  const batchTable = root.querySelector("#ss-batch-table");

  const map = L.map(root.querySelector("#ss-map")).setView([DEFAULT_LAT, DEFAULT_LON], 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap",
    maxZoom: 18,
  }).addTo(map);

  const flowLayer = L.geoJSON(null, {
    style: { color: "#7f9cb5", weight: 1.5, opacity: 0.85 },
  }).addTo(map);
  const basinLayer = L.geoJSON(null, {
    style: { color: "#2ca02c", weight: 2, fillColor: "#2ca02c", fillOpacity: 0.2 },
  }).addTo(map);
  const batchLayer = L.geoJSON(null, {
    style: { color: "#166534", weight: 2, fillColor: "#166534", fillOpacity: 0.15 },
  }).addTo(map);

  const selected = L.circleMarker([DEFAULT_LAT, DEFAULT_LON], {
    radius: 7,
    color: "#ff7f0e",
    fillColor: "#ff7f0e",
    fillOpacity: 0.95,
    weight: 2,
  }).addTo(map).bindTooltip("Selected");
  const snapped = L.circleMarker([DEFAULT_LAT, DEFAULT_LON], {
    radius: 7,
    color: "#1f77b4",
    fillColor: "#1f77b4",
    fillOpacity: 0.95,
    weight: 2,
  }).addTo(map).bindTooltip("Snapped");
  snapped.remove();

  let lastBasin = null;
  let batchResults = [];

  function pointFromInputs() {
    const latitude = toCoord(latInput.value);
    const longitude = toCoord(lonInput.value);
    if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) {
      throw new Error("Enter a latitude and longitude first.");
    }
    return {
      latitude,
      longitude,
      region: regionInput.value,
      verify_ssl: sslInput.checked,
    };
  }

  function moveSelected(lat, lon) {
    selected.setLatLng([lat, lon]);
    selected.bringToFront();
  }

  function showError(err) {
    errorEl.innerHTML = err ? `<div class="error"></div>` : "";
    if (err) errorEl.querySelector(".error").textContent = err.message || String(err);
  }

  function renderSnap() {
    const labels = ["Requested", "Snapped", "Region", "Snappable", "HUC", "Warning"];
    return `<table>${labels.map((label) => `<tr><th>${label}</th><td></td></tr>`).join("")}</table>`;
  }

  function fillTable(table, rows) {
    rows.forEach(([, value], i) => {
      table.rows[i].cells[1].textContent = value;
    });
  }

  function showSingle(snap, watershed) {
    const bits = [];
    if (snap) {
      bits.push("<h3>Snapped pour point</h3>");
      bits.push(renderSnap());
      if (!snap.snappable) {
        bits.push(`<p class="warn">StreamStats says this point isn't snappable.</p>`);
      }
    }
    if (watershed) {
      bits.push("<h3>Watershed</h3>");
      bits.push(`<table>
        <tr><th>Geometry</th><td></td></tr>
        <tr><th>Vertices</th><td></td></tr>
        <tr><th>Shape_Area</th><td></td></tr>
        <tr><th>Warning</th><td></td></tr>
      </table>`);
      bits.push(`<div class="actions"><button id="ss-geo" type="button">Download GeoJSON</button></div>`);
      bits.push(`<p class="hint">Shape_Area is whatever SS-Delineate reports. Units not confirmed.</p>`);
    }
    resultEl.innerHTML = bits.join("");
    const tables = resultEl.querySelectorAll("table");
    if (snap && tables[0]) {
      fillTable(tables[0], [
        ["Requested", `${Number(snap.request_latitude).toFixed(6)}, ${Number(snap.request_longitude).toFixed(6)}`],
        ["Snapped", `${Number(snap.latitude).toFixed(6)}, ${Number(snap.longitude).toFixed(6)}`],
        ["Region", snap.region || ""],
        ["Snappable", String(snap.snappable)],
        ["HUC", snap.huc_id || ""],
        ["Warning", snap.warning || ""],
      ]);
    }
    if (watershed && tables[1]) {
      fillTable(tables[1], [
        ["Geometry", watershed.geometry_type || ""],
        ["Vertices", String(watershed.vertex_count ?? "")],
        ["Shape_Area", String(watershed.shape_area ?? "")],
        ["Warning", watershed.warning || ""],
      ]);
    }
    const geoBtn = resultEl.querySelector("#ss-geo");
    if (geoBtn && watershed) {
      geoBtn.addEventListener("click", () => {
        saveJson(watershed.basin_geojson, "watershed.geojson");
      });
    }
  }

  function placeSnap(snap) {
    snapped.setLatLng([snap.latitude, snap.longitude]);
    if (!map.hasLayer(snapped)) snapped.addTo(map);
    snapped.bringToFront();
  }

  function drawBasin(feature, fit) {
    basinLayer.clearLayers();
    basinLayer.addData(feature);
    if (fit) {
      const bounds = basinLayer.getBounds();
      if (bounds.isValid()) map.fitBounds(bounds, { padding: [24, 24] });
    }
    selected.bringToFront();
    if (map.hasLayer(snapped)) snapped.bringToFront();
  }

  map.on("click", (ev) => {
    const lat = Math.round(ev.latlng.lat * 1e6) / 1e6;
    const lon = Math.round(ev.latlng.lng * 1e6) / 1e6;
    latInput.value = String(lat);
    lonInput.value = String(lon);
    moveSelected(lat, lon);
  });

  function onTyped() {
    const lat = toCoord(latInput.value);
    const lon = toCoord(lonInput.value);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    moveSelected(lat, lon);
    if (!map.getBounds().contains([lat, lon])) map.panTo([lat, lon]);
  }
  latInput.addEventListener("change", onTyped);
  lonInput.addEventListener("change", onTyped);

  async function withBusy(button, label, fn) {
    showError(null);
    button.disabled = true;
    const previous = button.textContent;
    button.textContent = label;
    try {
      await fn();
    } catch (err) {
      showError(err);
    } finally {
      button.disabled = false;
      button.textContent = previous;
    }
  }

  root.querySelector("#ss-snap").addEventListener("click", (ev) => {
    withBusy(ev.currentTarget, "Snapping…", async () => {
      statusEl.textContent = "Snapping to the stream network…";
      const snap = await apiPost("/api/streamstats/snap", pointFromInputs());
      statusEl.textContent = "";
      // The old polygon belonged to the previous point.
      lastBasin = null;
      basinLayer.clearLayers();
      placeSnap(snap);
      showSingle(snap, null);
    });
  });

  root.querySelector("#ss-delin").addEventListener("click", (ev) => {
    withBusy(ev.currentTarget, "Delineating…", async () => {
      statusEl.textContent = "Delineating watershed… this can take most of a minute.";
      const watershed = await apiPost("/api/streamstats/watershed", pointFromInputs());
      statusEl.textContent = "";
      lastBasin = watershed;
      placeSnap(watershed.snap);
      drawBasin(watershed.basin_geojson, true);
      showSingle(watershed.snap, watershed);
    });
  });

  root.querySelector("#ss-flow").addEventListener("click", (ev) => {
    withBusy(ev.currentTarget, "Loading…", async () => {
      const b = map.getBounds();
      const q = new URLSearchParams({
        west: b.getWest(),
        south: b.getSouth(),
        east: b.getEast(),
        north: b.getNorth(),
      });
      const data = await apiGet(`/api/flowlines?${q}`);
      flowLayer.clearLayers();
      flowLayer.addData(data.geojson);
      flowLayer.bringToBack();
      statusEl.textContent = data.capped
        ? `Loaded ${data.count} streams (service cap of 2000 — zoom in for more).`
        : `Loaded ${data.count} streams.`;
    });
  });

  root.querySelector("#ss-batch").addEventListener("click", (ev) => {
    withBusy(ev.currentTarget, "Delineating…", async () => {
      const file = root.querySelector("#ss-csv").files[0];
      if (!file) throw new Error("Choose a CSV first.");
      const parsed = parseCsv(await file.text());
      batchNote.innerHTML = "";
      if (parsed.skipped.length) {
        batchNote.innerHTML += `<p class="warn">Skipped ${parsed.skipped.length} row(s): ${parsed.skipped.join("; ")}</p>`;
      }
      if (!parsed.points.length) throw new Error("No usable lat/lon rows in that file.");
      if (parsed.points.length > 25) {
        throw new Error(`${parsed.points.length} points is over the 25-point cap. Split the file.`);
      }
      if (parsed.points.length > 10) {
        batchNote.innerHTML += `<p class="warn">${parsed.points.length} points. Each delineation can take most of a minute.</p>`;
      }
      // One request per point: each gets its own timeout, a bad
      // point only fails its own row, and finished basins survive.
      const results = [];
      for (const [i, point] of parsed.points.entries()) {
        statusEl.textContent = `Delineating ${i + 1} of ${parsed.points.length}…`;
        const id = point.id || `row-${i + 1}`;
        try {
          const watershed = await apiPost(
            "/api/streamstats/watershed",
            {
              latitude: point.latitude,
              longitude: point.longitude,
              region: regionInput.value,
              verify_ssl: sslInput.checked,
            },
            150000,
          );
          results.push({ id, ok: true, error: "", watershed });
        } catch (err) {
          results.push({ id, ok: false, error: err.message || String(err), watershed: null });
        }
      }
      statusEl.textContent = "";
      batchResults = results;
      drawBatch(batchResults);
      renderBatch(batchResults);
    });
  });

  function drawBatch(results) {
    batchLayer.clearLayers();
    for (const row of results) {
      if (row.ok && row.watershed && row.watershed.basin_geojson) {
        batchLayer.addData(row.watershed.basin_geojson);
      }
    }
    const bounds = batchLayer.getBounds();
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [24, 24] });
  }

  function renderBatch(results) {
    const head = `<table>
      <tr><th>Id</th><th>Snap</th><th>Vertices</th><th>Error</th></tr>
      ${results.map(() => `<tr><td></td><td></td><td></td><td></td></tr>`).join("")}
    </table>
    <div class="actions">
      <button id="ss-fc" type="button">Download basins</button>
      <button id="ss-rcsv" type="button">Download results CSV</button>
    </div>`;
    batchTable.innerHTML = head;
    // Row 0 is the header.
    results.forEach((row, i) => {
      const tr = batchTable.querySelectorAll("tr")[i + 1];
      const snap = row.watershed && row.watershed.snap;
      tr.cells[0].textContent = row.id;
      tr.cells[1].textContent = snap
        ? `${Number(snap.latitude).toFixed(5)}, ${Number(snap.longitude).toFixed(5)}`
        : "";
      tr.cells[2].textContent = row.ok ? String(row.watershed.vertex_count ?? "") : "";
      tr.cells[3].textContent = row.error || "";
    });
    batchTable.querySelector("#ss-fc").addEventListener("click", () => {
      const features = results
        .filter((row) => row.ok && row.watershed && row.watershed.basin_geojson)
        .map((row) => {
          const feature = row.watershed.basin_geojson;
          return {
            ...feature,
            properties: { ...(feature.properties || {}), id: row.id },
          };
        });
      saveJson({ type: "FeatureCollection", features }, "watersheds.geojson");
    });
    batchTable.querySelector("#ss-rcsv").addEventListener("click", () => {
      const lines = ["id,request_lat,request_lon,snap_lat,snap_lon,snappable,vertices,area,error"];
      for (const row of results) {
        const ws = row.watershed || {};
        const snap = ws.snap || {};
        lines.push([
          csvCell(row.id),
          snap.request_latitude ?? "",
          snap.request_longitude ?? "",
          snap.latitude ?? "",
          snap.longitude ?? "",
          snap.snappable ?? "",
          ws.vertex_count ?? "",
          ws.shape_area ?? "",
          csvCell(row.error || ""),
        ].join(","));
      }
      saveBlob(new Blob([lines.join("\n")], { type: "text/csv" }), "watershed_results.csv");
    });
  }

  setTimeout(() => map.invalidateSize(), 0);

  return () => {
    map.remove();
  };
}

function csvCell(value) {
  const text = String(value ?? "");
  if (/[",\n]/.test(text)) return `"${text.replaceAll('"', '""')}"`;
  return text;
}

function splitCsvLine(line) {
  const out = [];
  let cur = "";
  let quoted = false;
  for (const ch of line) {
    if (ch === '"') {
      quoted = !quoted;
      continue;
    }
    if (ch === "," && !quoted) {
      out.push(cur.trim());
      cur = "";
      continue;
    }
    cur += ch;
  }
  out.push(cur.trim());
  return out;
}

/** Number from a text box or CSV cell. Blank is NaN, not 0. */
function toCoord(text) {
  const trimmed = String(text ?? "").trim();
  return trimmed === "" ? NaN : Number(trimmed);
}

function parseCsv(text) {
  // Excel's "CSV UTF-8" starts with a byte-order mark that
  // would otherwise stick to the first header name.
  const lines = text.replace(/^\uFEFF/, "").split(/\r?\n/).filter((line) => line.trim());
  if (!lines.length) return { points: [], skipped: ["empty file"] };
  const headers = splitCsvLine(lines[0]).map((h) => h.toLowerCase());
  const latIdx = headers.findIndex((h) => h === "lat" || h === "latitude");
  const lonIdx = headers.findIndex((h) => h === "lon" || h === "long" || h === "longitude");
  const idIdx = headers.findIndex((h) => h === "id" || h === "name");
  if (latIdx < 0 || lonIdx < 0) {
    return { points: [], skipped: ["need a lat/latitude column and a lon/long/longitude column"] };
  }
  const points = [];
  const skipped = [];
  for (let i = 1; i < lines.length; i++) {
    const cells = splitCsvLine(lines[i]);
    const lat = toCoord(cells[latIdx]);
    const lon = toCoord(cells[lonIdx]);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      skipped.push(`line ${i + 1}`);
      continue;
    }
    const point = { latitude: lat, longitude: lon };
    if (idIdx >= 0 && cells[idIdx]) point.id = cells[idIdx];
    points.push(point);
  }
  return { points, skipped };
}
