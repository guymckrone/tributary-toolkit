/**
 * DEM page.
 *
 * The map stays in the browser. List datasets asks 3DEP what
 * overlaps the box. It does not download a raster.
 */

import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { apiDemDownload, apiForm, apiGet, apiPost, saveBlob } from "./api.js";
import { initialView, watchMap } from "./mapview.js";

export function mountDem(root) {
  root.innerHTML = `
    <div class="layout">
      <div class="stack">
        <section class="panel">
          <h2>DEM</h2>
          <label for="dem-provider">Provider</label>
          <select id="dem-provider"></select>
          <p id="dem-desc" class="hint"></p>
          <label class="check">
            <input id="dem-ssl" type="checkbox" />
            Verify SSL
          </label>
          <p class="hint">Leave this off on the work network. 3DEP rejects the interceptor's certificate.</p>
          <h3>Area</h3>
          <p class="hint">Click Draw box, then click two corners. Nothing is downloaded until a button asks.</p>
          <div class="actions">
            <button id="dem-draw" type="button">Draw box</button>
          </div>
          <label for="dem-file">Outline</label>
          <input id="dem-file" type="file" accept=".zip,.geojson,.json,.kml,.kmz" />
          <p class="hint">Shapefile zip, GeoJSON, KML, or KMZ. A projected file is rejected. Drawing a box clears this outline.</p>
          <div class="actions">
            <button id="dem-outline" type="button">Use outline</button>
          </div>
          <h3>Datasets</h3>
          <p class="hint">Lists what overlaps this box. One dataset is the download. A fill only patches holes.</p>
          <div class="actions">
            <button id="dem-list" type="button">List datasets</button>
          </div>
          <div id="dem-collections" class="scroll"></div>
          <div class="actions">
            <button id="dem-coverage" type="button">Show coverage</button>
          </div>
          <label for="dem-fill">Fill</label>
          <select id="dem-fill">
            <option value="">None</option>
          </select>
          <p id="dem-fill-hint" class="hint"></p>
          <label for="dem-resolution">Resolution (m)</label>
          <input id="dem-resolution" type="text" value="best" />
          <p class="hint">best uses the selected dataset's own cell size. Or type meters, such as 10.</p>
          <div class="actions">
            <button id="dem-estimate" type="button">Estimate size</button>
            <button id="dem-download" type="button" disabled>Download</button>
          </div>
          <p id="dem-estimate-out" class="hint"></p>
          <p id="dem-status" class="status"></p>
          <div id="dem-error"></div>
        </section>
      </div>
      <section class="panel">
        <div id="dem-map" class="map"></div>
      </section>
    </div>
  `;

  const state = {
    providers: [],
    drawing: false,
    corner: null,
    bbox: null,
    clip: null,
    collections: [],
    collectionKey: null,
    estimate: null,
  };
  const providerSelect = root.querySelector("#dem-provider");
  const descEl = root.querySelector("#dem-desc");
  const sslInput = root.querySelector("#dem-ssl");
  const statusEl = root.querySelector("#dem-status");
  const errorEl = root.querySelector("#dem-error");
  const collectionsEl = root.querySelector("#dem-collections");
  const fillSelect = root.querySelector("#dem-fill");
  const fillHint = root.querySelector("#dem-fill-hint");
  const resolutionInput = root.querySelector("#dem-resolution");
  const estimateEl = root.querySelector("#dem-estimate-out");
  const downloadBtn = root.querySelector("#dem-download");

  const start = initialView("dem", { lat: 44.0, lng: -120.5, zoom: 6 });
  const map = L.map(root.querySelector("#dem-map")).setView([start.lat, start.lng], start.zoom);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap",
    maxZoom: 18,
  }).addTo(map);
  let boxLayer = null;
  let rubber = null;
  let outlineLayer = null;
  let coverageLayer = null;

  function showError(err) {
    errorEl.innerHTML = err ? `<div class="error"></div>` : "";
    if (err) errorEl.querySelector(".error").textContent = err.message || String(err);
  }

  function currentProvider() {
    return state.providers.find((p) => p.key === providerSelect.value);
  }

  function readVerifySsl() {
    return sslInput.checked;
  }

  function showProvider() {
    const provider = currentProvider();
    descEl.textContent = provider ? provider.description : "";
  }

  // List datasets, estimate, and download will call this.
  // The label swap is undone in finally, so a failure cannot
  // leave the button stuck.
  async function withBusy(button, label, fn) {
    showError(null);
    button.disabled = true;
    const previous = button.textContent;
    button.textContent = label;
    try {
      await fn();
    } catch (err) {
      showError(err);
      statusEl.textContent = "";
    } finally {
      button.disabled = false;
      button.textContent = previous;
    }
  }

  function bboxText(bbox) {
    const [west, south, east, north] = bbox;
    const n = (value) => value.toFixed(5);
    return `West ${n(west)}, south ${n(south)}, east ${n(east)}, north ${n(north)}.`;
  }

  function clearOutline() {
    if (outlineLayer) map.removeLayer(outlineLayer);
    outlineLayer = null;
    state.clip = null;
  }

  function showBox(bbox) {
    const [west, south, east, north] = bbox;
    if (boxLayer) map.removeLayer(boxLayer);
    if (rubber) map.removeLayer(rubber);
    rubber = null;
    boxLayer = L.rectangle([[south, west], [north, east]], {
      color: "#1f6feb",
      weight: 2,
      fillOpacity: 0.08,
    }).addTo(map);
    state.bbox = [west, south, east, north];
    clearDatasets();
  }

  function meters(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "";
    const text = Math.abs(n - Math.round(n)) < 0.05 ? String(Math.round(n)) : n.toFixed(2);
    return `${text} m`;
  }

  function pct(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "";
    const text = Math.abs(n - Math.round(n)) < 0.05 ? String(Math.round(n)) : n.toFixed(1);
    return `${text}%`;
  }

  function selectedCollection() {
    return state.collections.find((item) => item.key === state.collectionKey) || null;
  }

  function hideCoverage() {
    if (coverageLayer) map.removeLayer(coverageLayer);
    coverageLayer = null;
    root.querySelector("#dem-coverage").textContent = "Show coverage";
  }

  function showCoverage() {
    hideCoverage();
    const item = selectedCollection();
    if (!item || !item.footprint) return;
    coverageLayer = L.geoJSON(item.footprint, {
      style: {
        color: "#6b3fa0",
        weight: 2,
        dashArray: "6 4",
        fillOpacity: 0.06,
      },
    }).addTo(map);
    if (boxLayer) boxLayer.bringToFront();
    if (outlineLayer) outlineLayer.bringToFront();
    root.querySelector("#dem-coverage").textContent = "Hide coverage";
  }

  function clearDatasets() {
    state.collections = [];
    state.collectionKey = null;
    collectionsEl.innerHTML = "";
    fillSelect.innerHTML = `<option value="">None</option>`;
    fillHint.textContent = "";
    hideCoverage();
    clearEstimate();
  }

  function clearEstimate() {
    state.estimate = null;
    estimateEl.className = "hint";
    estimateEl.textContent = "";
    downloadBtn.disabled = true;
  }

  function resolutionValue() {
    const raw = resolutionInput.value.trim().toLowerCase();
    if (!raw || raw === "best") return "best";
    const n = Number(raw);
    if (!Number.isFinite(n) || n <= 0) {
      throw new Error("Resolution is best, or a cell size in meters.");
    }
    return n;
  }

  function showEstimate(data) {
    state.estimate = data;
    const mb = Number(data.estimated_mb);
    const mbText = mb < 10 ? mb.toFixed(2) : mb.toFixed(1);
    const line = `${data.width_px} × ${data.height_px} px, about ${mbText} MB, ${meters(data.resolution_m)} cells, ${data.n_requests} request(s).`;
    if (data.over_cap) {
      estimateEl.className = "warn";
      estimateEl.textContent = `${line} Over the 250 MB cap. Download stays off.`;
    } else {
      estimateEl.className = "hint";
      estimateEl.textContent = `${line} Under the cap.`;
    }
    downloadBtn.disabled = Boolean(data.over_cap);
  }

  function demBody() {
    return {
      bbox: state.bbox,
      resolution_m: resolutionValue(),
      provider: providerSelect.value || "3dep",
      verify_ssl: readVerifySsl(),
      collection_key: state.collectionKey,
      fill_key: fillSelect.value || null,
    };
  }

  function downloadSummary(file) {
    const info = file.dem;
    const cache = info.fromCache === "true" ? "From cache." : "Downloaded.";
    const name = info.collectionName || info.provider || "DEM";
    const period = info.collectionPeriod ? ` (${info.collectionPeriod})` : "";
    const fill = info.fillUsed === "true" && info.fillName
      ? ` Fill ${info.fillName}${info.fillPeriod ? ` (${info.fillPeriod})` : ""}.`
      : "";
    const clip = info.clipName ? ` Clip ${info.clipName}.` : "";
    return `${name}${period}. ${info.requestedResolution} m requested, ${info.sourceResolution} m source. EPSG ${info.epsg}. ${info.verticalUnit}. Nodata ${info.nodata}. ${cache}${fill}${clip} ${info.description} → ${file.filename}`;
  }

  function renderFill() {
    const previous = fillSelect.value;
    const primary = selectedCollection();
    fillSelect.innerHTML = "";
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "None";
    fillSelect.appendChild(none);
    for (const item of state.collections) {
      if (item.key === state.collectionKey) continue;
      const option = document.createElement("option");
      option.value = item.key;
      option.textContent = `${item.name} (${item.period_label})`;
      fillSelect.appendChild(option);
    }
    const stillThere = [...fillSelect.options].some((opt) => opt.value === previous);
    fillSelect.value = stillThere ? previous : "";
    if (primary && primary.covers_all) {
      fillHint.textContent = "This dataset covers the whole box. A fill is recorded and then skipped.";
    } else if (primary) {
      fillHint.textContent = "A fill is copied in only where this dataset has no data.";
    } else {
      fillHint.textContent = "";
    }
  }

  function renderCollections() {
    collectionsEl.innerHTML = "";
    if (state.collections.length) {
      const table = document.createElement("table");
      const head = document.createElement("tr");
      for (const label of ["", "Dataset", "Years", "Cell", "Cover"]) {
        const th = document.createElement("th");
        th.textContent = label;
        head.appendChild(th);
      }
      table.appendChild(head);
      for (const item of state.collections) {
        const tr = document.createElement("tr");
        if (item.key === state.collectionKey) tr.className = "selected";
        const pick = document.createElement("td");
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = "dem-collection";
        radio.value = item.key;
        radio.checked = item.key === state.collectionKey;
        radio.addEventListener("change", () => {
          state.collectionKey = item.key;
          const showing = Boolean(coverageLayer);
          renderCollections();
          clearEstimate();
          if (showing) showCoverage();
        });
        pick.appendChild(radio);
        tr.appendChild(pick);
        for (const text of [
          item.name,
          item.period_label,
          meters(item.resolution_m),
          pct(item.coverage_pct),
        ]) {
          const td = document.createElement("td");
          td.textContent = text || "";
          tr.appendChild(td);
        }
        table.appendChild(tr);
      }
      collectionsEl.appendChild(table);
    }
    renderFill();
  }

  providerSelect.addEventListener("change", () => {
    showProvider();
    clearDatasets();
  });

  root.querySelector("#dem-draw").addEventListener("click", () => {
    state.drawing = true;
    state.corner = null;
    statusEl.textContent = "Click the first corner, then the opposite corner.";
  });

  // Two clicks. The first stores a corner. The second closes the
  // box. latLngBounds sorts the corners, so click order does not matter.
  map.on("click", (ev) => {
    if (!state.drawing) return;
    if (!state.corner) {
      state.corner = ev.latlng;
      return;
    }
    const bounds = L.latLngBounds(state.corner, ev.latlng);
    clearOutline();
    root.querySelector("#dem-file").value = "";
    showBox([bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()]);
    state.drawing = false;
    state.corner = null;
    statusEl.textContent = `Box ready. ${bboxText(state.bbox)}`;
  });

  map.on("mousemove", (ev) => {
    if (!state.drawing || !state.corner) return;
    const bounds = L.latLngBounds(state.corner, ev.latlng);
    if (rubber) map.removeLayer(rubber);
    rubber = L.rectangle(bounds, {
      color: "#1f6feb",
      weight: 1,
      dashArray: "4 4",
      fillOpacity: 0.05,
    }).addTo(map);
  });

  root.querySelector("#dem-outline").addEventListener("click", (ev) => {
    withBusy(ev.currentTarget, "Reading…", async () => {
      const file = root.querySelector("#dem-file").files[0];
      if (!file) throw new Error("Choose a shapefile zip, GeoJSON, KML, or KMZ first.");
      const form = new FormData();
      form.append("file", file);
      const data = await apiForm("/api/dem/boundary", form);
      state.drawing = false;
      state.corner = null;
      clearOutline();
      showBox(data.bbox);
      state.clip = { name: data.name, footprint: data.footprint };
      outlineLayer = L.geoJSON(data.footprint, {
        style: { color: "#c45c26", weight: 2, fillOpacity: 0.15 },
      }).addTo(map);
      const bounds = outlineLayer.getBounds();
      if (bounds.isValid()) map.fitBounds(bounds, { padding: [24, 24] });
      statusEl.textContent = `${data.name}. The rectangle is the download. The outline is the clip. ${bboxText(state.bbox)}`;
    });
  });

  fillSelect.addEventListener("change", clearEstimate);
  resolutionInput.addEventListener("input", clearEstimate);

  root.querySelector("#dem-estimate").addEventListener("click", (ev) => {
    withBusy(ev.currentTarget, "Estimating…", async () => {
      if (!state.bbox) throw new Error("Draw a box or use an outline first.");
      if (!state.collectionKey) throw new Error("List datasets and pick one.");
      statusEl.textContent = "Estimating the rectangle. Nothing is downloaded.";
      const data = await apiPost("/api/dem/estimate", demBody(), 300000);
      showEstimate(data);
      statusEl.textContent = "";
    });
  });

  downloadBtn.addEventListener("click", (ev) => {
    withBusy(ev.currentTarget, "Downloading…", async () => {
      if (!state.estimate || state.estimate.over_cap) {
        throw new Error("Estimate the size first. Over the cap stays off.");
      }
      statusEl.textContent = "Downloading the GeoTIFF…";
      const body = demBody();
      if (state.clip) {
        body.clip = { name: state.clip.name, footprint: state.clip.footprint };
      }
      const file = await apiDemDownload("/api/dem/download", body);
      saveBlob(file.blob, file.filename);
      statusEl.textContent = downloadSummary(file);
    });
  });

  root.querySelector("#dem-coverage").addEventListener("click", () => {
    if (coverageLayer) {
      hideCoverage();
      return;
    }
    const item = selectedCollection();
    if (!item) {
      showError(new Error("List datasets, then pick a row."));
      return;
    }
    if (!item.footprint) {
      showError(new Error("That dataset has no footprint to draw."));
      return;
    }
    showError(null);
    showCoverage();
  });

  root.querySelector("#dem-list").addEventListener("click", (ev) => {
    withBusy(ev.currentTarget, "Listing…", async () => {
      if (!state.bbox) throw new Error("Draw a box or use an outline first.");
      statusEl.textContent = "Asking 3DEP what overlaps this box…";
      const data = await apiPost("/api/dem/collections", {
        bbox: state.bbox,
        provider: providerSelect.value || "3dep",
        verify_ssl: readVerifySsl(),
      }, 300000);
      const wasShowing = Boolean(coverageLayer);
      state.collections = data.collections || [];
      const suggested = data.suggested_key || "";
      const known = state.collections.some((item) => item.key === suggested);
      state.collectionKey = known
        ? suggested
        : (state.collections[0] && state.collections[0].key) || null;
      renderCollections();
      clearEstimate();
      if (wasShowing) showCoverage();
      statusEl.textContent = state.collections.length
        ? `${state.collections.length} dataset(s). The highlighted row is the suggestion.`
        : "No datasets overlap this box.";
    });
  });

  apiGet("/api/dem/providers").then((data) => {
    state.providers = data.providers || [];
    providerSelect.innerHTML = state.providers
      .map((p) => `<option value="${p.key}">${p.display_name}</option>`)
      .join("");
    showProvider();
  }).catch((err) => {
    showError(new Error(`${err.message} — is uvicorn running on port 8000?`));
  });

  const stopWatch = watchMap(map, "dem");
  return () => {
    stopWatch();
    map.remove();
  };
}
