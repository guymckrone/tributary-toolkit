/**
 * DEM page.
 *
 * The map stays in the browser. Later buttons are the only
 * calls to 3DEP. The provider list is the catalog, not a download.
 */

import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { apiForm, apiGet } from "./api.js";
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
  };
  const providerSelect = root.querySelector("#dem-provider");
  const descEl = root.querySelector("#dem-desc");
  const sslInput = root.querySelector("#dem-ssl");
  const statusEl = root.querySelector("#dem-status");
  const errorEl = root.querySelector("#dem-error");

  const start = initialView("dem", { lat: 44.0, lng: -120.5, zoom: 6 });
  const map = L.map(root.querySelector("#dem-map")).setView([start.lat, start.lng], start.zoom);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap",
    maxZoom: 18,
  }).addTo(map);
  let boxLayer = null;
  let rubber = null;
  let outlineLayer = null;

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
  }

  providerSelect.addEventListener("change", showProvider);

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
