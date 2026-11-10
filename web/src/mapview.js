/**
 * Where the three tabs are aimed.
 *
 * Sync on: one center and zoom. I write it down after a pan,
 * not when the page first opens, so StreamStats can still
 * start on Johnson Creek and the other two on the state.
 * Sync off: each tab keeps its own. Unchecking copies the
 * map I'm looking at into all three, then they split.
 * Refresh clears this. The maps themselves are still thrown
 * away on a tab change.
 */

let sync = true;
let shared = null;
const own = {
  streamstats: null,
  download: null,
  dem: null,
};
let reader = null;

function copy(view) {
  return { lat: view.lat, lng: view.lng, zoom: view.zoom };
}

function remember(page, lat, lng, zoom) {
  const saved = { lat, lng, zoom };
  if (sync) shared = saved;
  else own[page] = saved;
}

export function initialView(page, fallback) {
  if (sync && shared) return copy(shared);
  if (!sync && own[page]) return copy(own[page]);
  return copy(fallback);
}

export function setSync(on) {
  const here = reader ? reader() : null;
  sync = Boolean(on);
  if (sync) {
    if (here) shared = copy(here);
    own.streamstats = null;
    own.download = null;
    own.dem = null;
    return;
  }
  if (!here) return;
  own.streamstats = copy(here);
  own.download = copy(here);
  own.dem = copy(here);
}

export function watchMap(map, page) {
  let alive = true;
  function onMove() {
    const center = map.getCenter();
    remember(page, center.lat, center.lng, map.getZoom());
  }
  function read() {
    const center = map.getCenter();
    return { lat: center.lat, lng: center.lng, zoom: map.getZoom() };
  }
  reader = read;
  // invalidateSize can fire moveend. I listen after it, so
  // opening the page does not count as a pan.
  const timer = setTimeout(() => {
    if (!alive) return;
    map.invalidateSize();
    map.on("moveend", onMove);
  }, 0);
  return () => {
    alive = false;
    clearTimeout(timer);
    map.off("moveend", onMove);
    if (reader === read) reader = null;
  };
}
