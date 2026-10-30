/**
 * Data Download page.
 *
 * Find sites / parameters / download are the only API calls.
 * Include, exclude, and search filter the loaded list here.
 * Pin clicks only change the selection.
 */

import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "leaflet.markercluster";
import "leaflet.markercluster/dist/MarkerCluster.css";
import "leaflet.markercluster/dist/MarkerCluster.Default.css";
import { apiDownload, apiForm, apiGet, apiPost, saveBlob } from "./api.js";

const COLORS = {
  USGS: "#1f77b4",
  Aquarius: "#2ca02c",
  SNOTEL: "#d62728",
};

export function mountDownload(root) {
  root.innerHTML = `
    <div class="layout">
      <div class="stack">
        <section class="panel">
          <h2>Source</h2>
          <label for="dl-source">Data source</label>
          <select id="dl-source"></select>
          <p id="dl-desc" class="hint"></p>
          <div id="dl-options"></div>
          <div id="dl-usgs"></div>
          <label for="dl-state">State</label>
          <input id="dl-state" type="text" value="OR" />
          <label for="dl-ids">Site IDs (optional)</label>
          <input id="dl-ids" type="text" placeholder="14211400 or HYDRA-1 or 302:OR:SNTL" />
          <div class="actions">
            <button id="dl-find" class="primary" type="button">Find sites</button>
          </div>
          <h3>Find by map area</h3>
          <p class="hint">Click Draw box, then click two corners. A projected shapefile is rejected.</p>
          <div class="actions">
            <button id="dl-draw" type="button">Draw box</button>
            <button id="dl-find-box" type="button">Find sites in box</button>
          </div>
          <label for="dl-zip">Shapefile ZIP</label>
          <input id="dl-zip" type="file" accept=".zip,application/zip" />
          <div class="actions">
            <button id="dl-shp" type="button">Use shapefile bbox</button>
          </div>
          <p id="dl-status" class="status"></p>
          <div id="dl-error"></div>
        </section>
        <section class="panel">
          <h3>Filter loaded sites</h3>
          <label for="dl-match">Match on</label>
          <select id="dl-match">
            <option value="id_or_name">ID or name</option>
            <option value="id">ID</option>
            <option value="name">Name</option>
          </select>
          <label for="dl-include">Include</label>
          <input id="dl-include" type="text" placeholder="comma separated, any match" />
          <label for="dl-exclude">Exclude</label>
          <input id="dl-exclude" type="text" />
          <label for="dl-search">Search</label>
          <input id="dl-search" type="text" />
          <p id="dl-count" class="hint"></p>
        </section>
      </div>
      <div class="stack">
        <section class="panel">
          <div id="dl-map" class="map"></div>
          <p class="hint">USGS blue · Aquarius green · SNOTEL red. Click a pin to select it.</p>
        </section>
        <section class="panel">
          <h3>Sites</h3>
          <div id="dl-sites" class="scroll"></div>
          <div class="actions">
            <button id="dl-params" type="button">Show available parameters</button>
          </div>
          <div id="dl-param-table" class="scroll"></div>
          <h3>Download</h3>
          <div class="row">
            <div>
              <label for="dl-start">Start date</label>
              <input id="dl-start" type="date" />
            </div>
            <div>
              <label for="dl-end">End date</label>
              <input id="dl-end" type="date" />
            </div>
          </div>
          <label for="dl-format">File</label>
          <select id="dl-format">
            <option value="csv">CSV</option>
            <option value="sqlite">SQLite</option>
          </select>
          <label for="dl-layout">Layout</label>
          <select id="dl-layout">
            <option value="combined">One file (all series together)</option>
            <option value="per_series">One file per series (ZIP)</option>
          </select>
          <div class="actions">
            <button id="dl-go" class="primary" type="button">Download data</button>
          </div>
          <p id="dl-down-status" class="status"></p>
        </section>
      </div>
    </div>
  `;

  const state = {
    sources: [],
    sites: [],
    selected: new Set(),
    params: [],
    bbox: null,
    drawing: false,
    corner: null,
  };

  const sourceSelect = root.querySelector("#dl-source");
  const descEl = root.querySelector("#dl-desc");
  const optionsEl = root.querySelector("#dl-options");
  const usgsEl = root.querySelector("#dl-usgs");
  const statusEl = root.querySelector("#dl-status");
  const errorEl = root.querySelector("#dl-error");
  const countEl = root.querySelector("#dl-count");
  const sitesEl = root.querySelector("#dl-sites");
  const paramsEl = root.querySelector("#dl-param-table");
  const downStatus = root.querySelector("#dl-down-status");

  const today = new Date();
  const yearAgo = new Date(today);
  yearAgo.setFullYear(today.getFullYear() - 1);
  root.querySelector("#dl-start").value = isoDate(yearAgo);
  root.querySelector("#dl-end").value = isoDate(today);

  const map = L.map(root.querySelector("#dl-map")).setView([44.0, -120.5], 6);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap",
    maxZoom: 18,
  }).addTo(map);
  const cluster = L.markerClusterGroup({ maxClusterRadius: 50, showCoverageOnHover: false });
  map.addLayer(cluster);
  let boxLayer = null;
  let rubber = null;

  function showError(err) {
    errorEl.innerHTML = err ? `<div class="error"></div>` : "";
    if (err) errorEl.querySelector(".error").textContent = err.message || String(err);
  }

  function currentKey() {
    return sourceSelect.value;
  }

  function currentSource() {
    return state.sources.find((s) => s.key === currentKey());
  }

  function readVerifySsl() {
    const box = optionsEl.querySelector('[data-opt="verify_ssl"]');
    if (!box) return true;
    return box.checked;
  }

  function renderOptions() {
    const src = currentSource();
    descEl.textContent = src ? src.description : "";
    optionsEl.innerHTML = "";
    usgsEl.innerHTML = "";
    for (const opt of (src && src.options) || []) {
      // Each catalogue row already carries its own duration.
      // A dropdown here would look like it controls the download.
      if (opt.key === "duration") {
        optionsEl.innerHTML += `<p class="hint">Duration comes from each series you pick (daily, hourly, …), not from one default.</p>`;
        continue;
      }
      const id = `opt-${opt.key}`;
      if (opt.kind === "checkbox") {
        optionsEl.innerHTML += `<label class="check"><input id="${id}" data-opt="${opt.key}" type="checkbox" ${opt.default ? "checked" : ""}/> ${opt.label}</label>`;
      } else if (opt.kind === "select") {
        const choices = opt.choices || [];
        optionsEl.innerHTML += `<label for="${id}">${opt.label}</label><select id="${id}" data-opt="${opt.key}">${choices.map((c) => `<option ${c === opt.default ? "selected" : ""}>${c}</option>`).join("")}</select>`;
      } else if (opt.kind === "number") {
        optionsEl.innerHTML += `<label for="${id}">${opt.label}</label><input id="${id}" data-opt="${opt.key}" type="number" value="${opt.default ?? 0}" />`;
      } else {
        optionsEl.innerHTML += `<label for="${id}">${opt.label}</label><input id="${id}" data-opt="${opt.key}" type="text" value="${opt.default ?? ""}" />`;
      }
      if (opt.help) optionsEl.innerHTML += `<p class="hint">${opt.help}</p>`;
    }
    if (currentKey() === "usgs") {
      usgsEl.innerHTML = `
        <div class="row">
          <div>
            <label for="dl-agency">Agency code</label>
            <input id="dl-agency" type="text" value="USGS" />
          </div>
          <div>
            <label for="dl-type">Site type code</label>
            <input id="dl-type" type="text" value="ST" />
          </div>
        </div>
        <p class="hint">USGS + ST keeps federal stream gages. Johnson Creek is 14211400.</p>
      `;
    } else if (currentKey() === "snotel") {
      usgsEl.innerHTML = `<p class="hint">Aneroid Lake #2 is 302:OR:SNTL. Bbox search still needs a state.</p>`;
    } else if (currentKey() === "aquarius") {
      usgsEl.innerHTML = `<p class="hint">HYDRA-1 is a precip test location. Include HYDRA after Find sites. Some datasets have no map pin.</p>`;
    }
  }

  function parseIds(raw) {
    const parts = String(raw || "").split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
    return parts.length ? parts : null;
  }

  function sitesBody(bbox) {
    const agency = root.querySelector("#dl-agency");
    const siteType = root.querySelector("#dl-type");
    return {
      state: root.querySelector("#dl-state").value.trim() || null,
      site_ids: bbox ? null : parseIds(root.querySelector("#dl-ids").value),
      bbox,
      agency_code: agency ? agency.value : "USGS",
      site_type_code: siteType ? siteType.value : "ST",
      verify_ssl: readVerifySsl(),
    };
  }

  function haystack(site) {
    const mode = root.querySelector("#dl-match").value;
    const id = String(site.site_id || "").toLowerCase();
    const name = String(site.name || "").toLowerCase();
    if (mode === "id") return id;
    if (mode === "name") return name;
    return `${id} ${name}`;
  }

  function patterns(raw) {
    return String(raw || "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
  }

  function filteredSites() {
    const include = patterns(root.querySelector("#dl-include").value);
    const exclude = patterns(root.querySelector("#dl-exclude").value);
    const search = root.querySelector("#dl-search").value.trim().toLowerCase();
    return state.sites.filter((site) => {
      const text = haystack(site);
      if (include.length && !include.some((p) => text.includes(p))) return false;
      if (exclude.some((p) => text.includes(p))) return false;
      if (search && !text.includes(search)) return false;
      return true;
    });
  }

  function renderSites(fit) {
    const rows = filteredSites();
    let skipped = 0;
    cluster.clearLayers();
    for (const site of rows) {
      const lat = Number(site.latitude);
      const lon = Number(site.longitude);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
        skipped += 1;
        continue;
      }
      const color = COLORS[site.source] || "#7f7f7f";
      const picked = state.selected.has(String(site.site_id));
      const marker = L.circleMarker([lat, lon], {
        radius: picked ? 9 : 6,
        color: picked ? "#111" : color,
        fillColor: color,
        fillOpacity: 0.9,
        weight: picked ? 3 : 1,
      });
      marker.bindTooltip(String(site.site_id));
      marker.on("click", (ev) => {
        L.DomEvent.stopPropagation(ev);
        toggle(site.site_id);
      });
      cluster.addLayer(marker);
    }
    const shownIds = new Set(rows.map((site) => String(site.site_id)));
    const selectedShown = [...state.selected].filter((id) => shownIds.has(id)).length;
    countEl.textContent = `${rows.length} site(s) shown` +
      (skipped ? `, ${skipped} without coordinates` : "") +
      `. ${selectedShown} selected.`;

    const header = ["site_id", "name", "source", "state", "latitude", "longitude"];
    sitesEl.innerHTML = `<table><tr>${header.map((h) => `<th>${h}</th>`).join("")}</tr>${rows.map(() => `<tr class="clickable">${header.map(() => "<td></td>").join("")}</tr>`).join("")}</table>`;
    const trs = sitesEl.querySelectorAll("tr");
    rows.forEach((site, i) => {
      const tr = trs[i + 1];
      header.forEach((key, col) => {
        tr.cells[col].textContent = site[key] ?? "";
      });
      tr.classList.toggle("selected", state.selected.has(String(site.site_id)));
      tr.addEventListener("click", () => toggle(site.site_id));
    });
    if (fit && rows.length) {
      const lats = rows.map((s) => Number(s.latitude)).filter(Number.isFinite);
      const lons = rows.map((s) => Number(s.longitude)).filter(Number.isFinite);
      if (lats.length) {
        map.fitBounds([
          [Math.min(...lats), Math.min(...lons)],
          [Math.max(...lats), Math.max(...lons)],
        ], { padding: [24, 24], maxZoom: 12 });
      }
    }
  }

  function selectedShownIds() {
    const shown = new Set(filteredSites().map((site) => String(site.site_id)));
    return [...state.selected].filter((id) => shown.has(id));
  }

  function toggle(siteId) {
    const id = String(siteId);
    if (state.selected.has(id)) state.selected.delete(id);
    else state.selected.add(id);
    renderSites(false);
  }

  async function loadSites(bbox) {
    showError(null);
    statusEl.textContent = "Finding sites…";
    const data = await apiPost(`/api/sources/${currentKey()}/sites`, sitesBody(bbox));
    state.sites = data.sites || [];
    state.selected = new Set();
    state.params = [];
    paramsEl.innerHTML = "";
    statusEl.textContent = `${data.count} site(s).`;
    renderSites(true);
  }

  function renderParams() {
    const rows = state.params;
    if (!rows.length) {
      paramsEl.innerHTML = `<p class="hint">No parameter rows.</p>`;
      return;
    }
    // label / statistic_id / sublocation / height_depth tell apart
    // series that otherwise share a site and code.
    const preferred = [
      "site_id", "code", "name", "label", "unit", "computation",
      "statistic_id", "sublocation", "duration", "ordinal",
      "height_depth", "begin", "end",
    ];
    const keys = preferred.filter((key) => rows.some((row) => row[key]));
    paramsEl.innerHTML = `<table><tr><th></th>${keys.map((k) => `<th>${k}</th>`).join("")}</tr>${rows.map(() => `<tr><td><input type="checkbox" class="series-pick" /></td>${keys.map(() => "<td></td>").join("")}</tr>`).join("")}</table>`;
    const trs = paramsEl.querySelectorAll("tr");
    rows.forEach((row, i) => {
      const tr = trs[i + 1];
      keys.forEach((key, col) => {
        tr.cells[col + 1].textContent = row[key] ?? "";
      });
    });
  }

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

  sourceSelect.addEventListener("change", () => {
    state.sites = [];
    state.selected = new Set();
    state.params = [];
    state.bbox = null;
    state.drawing = false;
    state.corner = null;
    if (boxLayer) map.removeLayer(boxLayer);
    if (rubber) map.removeLayer(rubber);
    boxLayer = null;
    rubber = null;
    paramsEl.innerHTML = "";
    sitesEl.innerHTML = "";
    renderOptions();
    renderSites(false);
  });

  ["#dl-include", "#dl-exclude", "#dl-search", "#dl-match"].forEach((sel) => {
    root.querySelector(sel).addEventListener("input", () => renderSites(false));
    root.querySelector(sel).addEventListener("change", () => renderSites(false));
  });

  root.querySelector("#dl-find").addEventListener("click", (ev) => {
    withBusy(ev.currentTarget, "Finding…", () => loadSites(null));
  });

  root.querySelector("#dl-find-box").addEventListener("click", (ev) => {
    withBusy(ev.currentTarget, "Finding…", async () => {
      if (!state.bbox) throw new Error("Draw a rectangle on the map first.");
      await loadSites(state.bbox);
    });
  });

  root.querySelector("#dl-draw").addEventListener("click", () => {
    state.drawing = true;
    state.corner = null;
    statusEl.textContent = "Click the first corner, then the opposite corner.";
  });

  map.on("click", (ev) => {
    if (!state.drawing) return;
    if (!state.corner) {
      state.corner = ev.latlng;
      return;
    }
    const bounds = L.latLngBounds(state.corner, ev.latlng);
    if (boxLayer) map.removeLayer(boxLayer);
    if (rubber) map.removeLayer(rubber);
    boxLayer = L.rectangle(bounds, { color: "#1f6feb", weight: 2, fillOpacity: 0.08 }).addTo(map);
    state.bbox = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()];
    state.drawing = false;
    state.corner = null;
    statusEl.textContent = "Box ready. Click Find sites in box.";
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

  root.querySelector("#dl-shp").addEventListener("click", (ev) => {
    withBusy(ev.currentTarget, "Reading…", async () => {
      const file = root.querySelector("#dl-zip").files[0];
      if (!file) throw new Error("Choose a shapefile ZIP first.");
      const form = new FormData();
      form.append("file", file);
      const data = await apiForm("/api/sources/shapefile-bbox", form);
      state.bbox = data.bbox;
      const [west, south, east, north] = data.bbox;
      if (boxLayer) map.removeLayer(boxLayer);
      boxLayer = L.rectangle([[south, west], [north, east]], {
        color: "#1f6feb",
        weight: 2,
        fillOpacity: 0.08,
      }).addTo(map);
      map.fitBounds(boxLayer.getBounds(), { padding: [24, 24] });
      statusEl.textContent = data.note || "Shapefile bbox ready.";
      await loadSites(state.bbox);
    });
  });

  root.querySelector("#dl-params").addEventListener("click", (ev) => {
    withBusy(ev.currentTarget, "Loading…", async () => {
      const ids = selectedShownIds();
      if (!ids.length) throw new Error("Select at least one site (click a pin or a table row).");
      statusEl.textContent = "Loading parameters…";
      const data = await apiPost(`/api/sources/${currentKey()}/parameters`, {
        site_ids: ids,
        verify_ssl: readVerifySsl(),
      });
      state.params = data.parameters || [];
      statusEl.textContent = `${data.count} series row(s).`;
      renderParams();
    });
  });

  root.querySelector("#dl-go").addEventListener("click", (ev) => {
    withBusy(ev.currentTarget, "Downloading…", async () => {
      const picks = [...paramsEl.querySelectorAll(".series-pick")]
        .map((box, i) => (box.checked ? state.params[i] : null))
        .filter(Boolean);
      if (!picks.length) throw new Error("Pick at least one series.");
      downStatus.textContent = "Downloading…";
      const file = await apiDownload(`/api/sources/${currentKey()}/download`, {
        start_date: root.querySelector("#dl-start").value,
        end_date: root.querySelector("#dl-end").value,
        series: picks,
        file_format: root.querySelector("#dl-format").value,
        layout: root.querySelector("#dl-layout").value,
        verify_ssl: readVerifySsl(),
      });
      saveBlob(file.blob, file.filename);
      downStatus.textContent = `${file.pointCount} points from ${file.seriesCount} series → ${file.filename}`;
      if (file.errors) showError(new Error(file.errors));
    });
  });

  apiGet("/api/sources").then((data) => {
    state.sources = data.sources || [];
    sourceSelect.innerHTML = state.sources
      .map((s) => `<option value="${s.key}">${s.display_name}</option>`)
      .join("");
    renderOptions();
  }).catch((err) => {
    showError(new Error(`${err.message} — is uvicorn running on port 8000?`));
  });

  setTimeout(() => map.invalidateSize(), 0);
  return () => map.remove();
}

function isoDate(d) {
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${month}-${day}`;
}
