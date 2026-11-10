import "./styles.css";
import { mountStreamStats } from "./streamstats.js";
import { mountDownload } from "./download.js";
import { mountDem } from "./dem.js";
import { setSync } from "./mapview.js";

const page = document.getElementById("page");
let cleanup = null;

function show(name) {
  if (cleanup) {
    cleanup();
    cleanup = null;
  }
  page.innerHTML = "";
  document.querySelectorAll("nav a").forEach((a) => {
    a.classList.toggle("active", a.dataset.page === name);
  });
  if (name === "download") {
    cleanup = mountDownload(page);
  } else if (name === "dem") {
    cleanup = mountDem(page);
  } else {
    cleanup = mountStreamStats(page);
  }
}

function fromHash() {
  const name = (location.hash || "#streamstats").replace("#", "");
  if (name === "download" || name === "dem") {
    show(name);
  } else {
    show("streamstats");
  }
}

window.addEventListener("hashchange", fromHash);
fromHash();

document.getElementById("sync-view").addEventListener("change", (ev) => {
  setSync(ev.target.checked);
});
