import "./styles.css";
import { mountStreamStats } from "./streamstats.js";
import { mountDownload } from "./download.js";

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
  } else {
    cleanup = mountStreamStats(page);
  }
}

function fromHash() {
  const name = (location.hash || "#streamstats").replace("#", "");
  show(name === "download" ? "download" : "streamstats");
}

window.addEventListener("hashchange", fromHash);
fromHash();
