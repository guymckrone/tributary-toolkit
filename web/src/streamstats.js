/**
 * StreamStats page.
 *
 * A click moves the orange marker, unless "Pick multiple points"
 * is on — then each click adds a numbered point and does not
 * call USGS. Snap and Delineate are the calls to Python.
 */

import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { apiDownload, apiPost, saveBlob } from "./api.js";

const DEFAULT_LAT = 45.3725;
const DEFAULT_LON = -122.5858;

// One control, used under a single basin and under a batch.
const FORMAT_PICKER = `<select class="ss-fmt" aria-label="Download format">
  <option value="geojson">GeoJSON</option>
  <option value="shp">Shapefile</option>
  <option value="kml">KML</option>
  <option value="kmz">KMZ</option>
</select>`;

export function mountStreamStats(root) {
  root.innerHTML = `
    <div class="layout">
      <div class="stack">
        <section class="panel">
          <h2>Pour point</h2>
          <label class="check">
            <input id="ss-multi" type="checkbox" />
            Pick multiple points
          </label>
          <label for="ss-region">Region (state code)</label>
          <input id="ss-region" type="text" value="OR" />
          <label class="check">
            <input id="ss-ssl" type="checkbox" checked />
            Verify SSL
          </label>
          <p class="hint">Turn this off on the work network. It covers snap and delineate.</p>
          <p id="ss-status" class="status"></p>
          <div id="ss-error"></div>
          <div id="ss-single">
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
            <div class="actions">
              <button id="ss-snap" type="button">Snap</button>
              <button id="ss-delin" class="primary" type="button">Delineate watershed</button>
            </div>
            <div id="ss-result"></div>
          </div>
          <div id="ss-multi-panel" hidden>
            <p class="hint">Each click adds a point. Drag a number to move it. Deleting pt-1 makes the next new point pt-1 again. Cap 25. Several basins download as one zip, because a browser will block a burst of separate downloads.</p>
            <div id="ss-picks" class="scroll"></div>
            <div class="actions">
              <button id="ss-pick-go" type="button">Delineate picked points</button>
              <button id="ss-pick-clear" type="button">Clear all</button>
            </div>
          </div>
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
        <p class="hint">Hydro tiles are a guide for aiming. Snap still uses SS-Delineate, which can see channels the tiles do not draw. The layer control can hide the tiles.</p>
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
  const streets = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap",
    maxZoom: 18,
  }).addTo(map);
  // ArcGIS cached tiles are /tile/{z}/{y}/{x}, not Leaflet's {z}/{x}/{y}.
  // Pre-drawn pictures, so a zoomed-out state still looks complete.
  const hydroTiles = L.tileLayer(
    "https://basemap.nationalmap.gov/arcgis/rest/services/USGSHydroCached/MapServer/tile/{z}/{y}/{x}",
    {
      maxNativeZoom: 16,
      maxZoom: 18,
      opacity: 0.7,
      attribution: "USGS The National Map",
    },
  ).addTo(map);

  L.control.layers(
    { Streets: streets },
    { "Hydro (tiles)": hydroTiles },
  ).addTo(map);
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
  // Clicks while "Pick multiple points" is on.
  let pickedPoints = [];
  // True after a snap, so the blue dot can come back when I
  // leave multi mode. Removing it for the mode switch is not
  // the same as the user clearing it.
  let snappedVisible = false;
  const pickLayer = L.featureGroup().addTo(map);

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
      bits.push(`<div class="actions">${FORMAT_PICKER}<button id="ss-geo" type="button">Download</button></div>`);
      bits.push(`<p class="hint">Shape_Area is whatever SS-Delineate reports. Units not confirmed. A shapefile arrives as a zip of the .shp, .shx, .dbf, and .prj files.</p>`);
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
        const format = resultEl.querySelector(".ss-fmt").value;
        downloadBasins([{ id: "watershed", watershed }], format).catch(showError);
      });
    }
  }

  function placeSnap(snap) {
    snapped.setLatLng([snap.latitude, snap.longitude]);
    snappedVisible = true;
    if (!root.querySelector("#ss-multi").checked && !map.hasLayer(snapped)) {
      snapped.addTo(map);
    }
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

  function nextPickId() {
    // Lowest pt-N that is not already in the list. Deleting pt-1
    // makes the next click pt-1 again. A renamed id does not hold a number.
    const used = new Set();
    for (const pt of pickedPoints) {
      const match = /^pt-(\d+)$/.exec(pt.id);
      if (match) used.add(Number(match[1]));
    }
    let n = 1;
    while (used.has(n)) n += 1;
    return `pt-${n}`;
  }

  function syncPickMode() {
    const multi = root.querySelector("#ss-multi").checked;
    root.querySelector("#ss-single").hidden = multi;
    root.querySelector("#ss-multi-panel").hidden = !multi;
    if (multi) {
      if (map.hasLayer(selected)) selected.remove();
      if (map.hasLayer(snapped)) snapped.remove();
      if (map.hasLayer(basinLayer)) map.removeLayer(basinLayer);
      drawPicks();
      return;
    }
    pickLayer.clearLayers();
    if (!map.hasLayer(selected)) selected.addTo(map);
    if (!map.hasLayer(basinLayer)) basinLayer.addTo(map);
    if (snappedVisible && !map.hasLayer(snapped)) snapped.addTo(map);
    selected.bringToFront();
  }

  map.on("click", (ev) => {
    const lat = Math.round(ev.latlng.lat * 1e6) / 1e6;
    const lon = Math.round(ev.latlng.lng * 1e6) / 1e6;
    if (root.querySelector("#ss-multi").checked) {
      if (pickedPoints.length >= 25) {
        showError(new Error("25 points is the cap. Delineate these or remove one."));
        return;
      }
      showError(null);
      readPickIds();
      pickedPoints.push({ id: nextPickId(), latitude: lat, longitude: lon });
      drawPicks();
      renderPicks();
      return;
    }
    latInput.value = String(lat);
    lonInput.value = String(lon);
    moveSelected(lat, lon);
  });

  root.querySelector("#ss-multi").addEventListener("change", syncPickMode);

  function drawPicks() {
    pickLayer.clearLayers();
    if (!root.querySelector("#ss-multi").checked) return;
    pickedPoints.forEach((pt, i) => {
      const num = /^pt-(\d+)$/.exec(pt.id);
      const marker = L.marker([pt.latitude, pt.longitude], {
        draggable: true,
        icon: L.divIcon({
          className: "leaflet-div-icon pick-num",
          html: num ? num[1] : String(i + 1),
          iconSize: [22, 22],
          iconAnchor: [11, 11],
        }),
      });
      marker.bindTooltip(pt.id);
      // A drag should not also count as a new map click.
      marker.on("click", (ev) => L.DomEvent.stopPropagation(ev));
      marker.on("dragend", () => {
        const ll = marker.getLatLng();
        pt.latitude = Math.round(ll.lat * 1e6) / 1e6;
        pt.longitude = Math.round(ll.lng * 1e6) / 1e6;
        const tr = root.querySelectorAll("#ss-picks tr")[i + 1];
        if (!tr) return;
        tr.cells[2].textContent = pt.latitude.toFixed(5);
        tr.cells[3].textContent = pt.longitude.toFixed(5);
      });
      marker.addTo(pickLayer);
    });
  }

  function renderPicks() {
    const box = root.querySelector("#ss-picks");
    if (!pickedPoints.length) {
      box.innerHTML = "";
      return;
    }
    box.innerHTML = `<table>
      <tr><th></th><th>Id</th><th>Lat</th><th>Lon</th><th></th></tr>
      ${pickedPoints.map(() => `<tr><td></td><td><input class="pick-id" /></td><td></td><td></td><td><button type="button" class="pick-x">Remove</button></td></tr>`).join("")}
    </table>`;
    pickedPoints.forEach((pt, i) => {
      const tr = box.querySelectorAll("tr")[i + 1];
      const num = /^pt-(\d+)$/.exec(pt.id);
      tr.cells[0].textContent = num ? num[1] : String(i + 1);
      const input = tr.querySelector(".pick-id");
      input.value = pt.id;
      input.addEventListener("change", () => {
        pt.id = input.value.trim() || pt.id;
        input.value = pt.id;
        drawPicks();
      });
      tr.cells[2].textContent = pt.latitude.toFixed(5);
      tr.cells[3].textContent = pt.longitude.toFixed(5);
      tr.querySelector(".pick-x").addEventListener("click", () => {
        pickedPoints.splice(i, 1);
        drawPicks();
        renderPicks();
      });
    });
  }

  function readPickIds() {
    root.querySelectorAll("#ss-picks .pick-id").forEach((input, i) => {
      if (!pickedPoints[i]) return;
      pickedPoints[i].id = input.value.trim() || pickedPoints[i].id;
    });
  }

  root.querySelector("#ss-pick-clear").addEventListener("click", () => {
    pickedPoints = [];
    drawPicks();
    renderPicks();
  });

  syncPickMode();

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

  // CSV rows and map picks both end here. One request per point:
  // a bad point only fails its own row, and finished basins stay.
  async function delineateMany(points) {
    if (!points.length) throw new Error("No points to delineate.");
    if (points.length > 25) {
      throw new Error(`${points.length} points is over the 25-point cap. Split them up.`);
    }
    if (points.length > 10) {
      batchNote.innerHTML += `<p class="warn">${points.length} points. Each delineation can take most of a minute.</p>`;
    }
    const results = [];
    for (const [i, point] of points.entries()) {
      statusEl.textContent = `Delineating ${i + 1} of ${points.length}…`;
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
    return results;
  }

  function finishBatch(results) {
    batchResults = results;
    drawBatch(batchResults);
    renderBatch(batchResults);
  }

  root.querySelector("#ss-pick-go").addEventListener("click", (ev) => {
    withBusy(ev.currentTarget, "Delineating…", async () => {
      readPickIds();
      batchNote.innerHTML = "";
      finishBatch(await delineateMany(pickedPoints.map((pt) => ({ ...pt }))));
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
      finishBatch(await delineateMany(parsed.points));
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
    pickLayer.bringToFront();
  }

  function renderBatch(results) {
    const head = `<table>
      <tr><th>Id</th><th>Snap</th><th>Vertices</th><th>Error</th></tr>
      ${results.map(() => `<tr><td></td><td></td><td></td><td></td></tr>`).join("")}
    </table>
    <div class="actions">
      ${FORMAT_PICKER}
      <button id="ss-fc" type="button">Download basins</button>
      <button id="ss-rcsv" type="button">Download results CSV</button>
    </div>
    <p class="hint">More than one basin comes back as a zip of separate files. A browser will block a burst of individual downloads.</p>`;
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
      const format = batchTable.querySelector(".ss-fmt").value;
      // Failed rows have no polygon. Leave them out.
      const basins = results
        .filter((row) => row.ok && row.watershed && row.watershed.basin_geojson)
        .map((row) => ({ id: row.id, watershed: row.watershed }));
      downloadBasins(basins, format).catch(showError);
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

/** Ask Python for the file. Several basins come back as one zip. */
async function downloadBasins(basins, format) {
  if (!basins.length) throw new Error("No watershed to download.");
  const result = await apiDownload("/api/streamstats/export", { format, basins });
  saveBlob(result.blob, result.filename);
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
