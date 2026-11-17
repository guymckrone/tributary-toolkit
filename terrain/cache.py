"""On-disk cache for terrain prep. No Whitebox, no network.

One folder per key under terrain/data/cache:

    <key>/breached.tif
    <key>/d8_pointer.tif
    <key>/d8_accum_cells.tif
    <key>/streams.tif
    <key>/prep.json

The key is the DEM's content hash plus the prep params as asked.
accum_fraction and accum_threshold are different entries even
when they land on the same cell count. The fraction is resolved
from the accumulation, and I don't want to run Whitebox just to
compute a key.

prep.json is written last. A crash partway through leaves no
sidecar, and the next call is a miss that overwrites the folder.
The JSON stores file names, not full paths, so the folder can
move.
"""

import hashlib
import json
import shutil
from dataclasses import fields, replace
from pathlib import Path
from typing import Optional

from .models import PrepParams, PrepResult

SIDECAR = "prep.json"
# Field name on PrepResult -> file name inside the key folder.
PREP_FILES = {
    "breached_path": "breached.tif",
    "pointer_path": "d8_pointer.tif",
    "accum_path": "d8_accum_cells.tif",
    "streams_path": "streams.tif",
}
# Bump when the prep steps change in a way that makes old files
# wrong. Old folders then miss and get rebuilt.
PREP_VERSION = 1

_NOT_STORED = ("from_cache",) + tuple(PREP_FILES)
_STORED = tuple(
    field.name for field in fields(PrepResult) if field.name not in _NOT_STORED
)


def default_cache_dir() -> Path:
    """terrain/data/cache next to this file. Gitignored except the README."""
    return Path(__file__).resolve().parent / "data" / "cache"


def prep_cache_key(dem_identity: str, params: PrepParams) -> str:
    """Same DEM bytes and same params, same key."""
    if params.accum_threshold is not None:
        threshold = f"cells={params.accum_threshold:.4f}"
    else:
        threshold = f"fraction={params.accum_fraction:.6f}"
    lines = [
        f"prep_version={PREP_VERSION}",
        f"dem={dem_identity}",
        f"threshold={threshold}",
        f"breach_dist={params.breach_dist}",
        f"fill_deps={params.fill_deps}",
        f"mainstem_only={params.mainstem_only}",
    ]
    canonical = "\n".join(lines)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def entry_dir(cache_dir: Path, key: str) -> Path:
    return Path(cache_dir) / key


def _tiff_magic_ok(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == b"II*\x00"
    except OSError:
        return False


def lookup_prep(cache_dir: Path, key: str) -> Optional[PrepResult]:
    """The cached prep, or None on a miss or a broken folder.

    A sidecar with any raster missing, or a raster that isn't a
    TIFF, is a miss.
    """
    folder = entry_dir(cache_dir, key)
    sidecar = folder / SIDECAR
    if not sidecar.is_file():
        return None
    paths = {name: folder / file_name for name, file_name in PREP_FILES.items()}
    if not all(_tiff_magic_ok(path) for path in paths.values()):
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        stored = {name: data[name] for name in _STORED if name in data}
        if stored.get("cache_key") != key:
            return None
        return PrepResult(
            from_cache=True,
            **{name: str(path) for name, path in paths.items()},
            **stored,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def store_prep(cache_dir: Path, result: PrepResult) -> PrepResult:
    """Write prep.json for rasters already in the key folder."""
    folder = entry_dir(cache_dir, result.cache_key)
    for name, file_name in PREP_FILES.items():
        expected = folder / file_name
        if Path(getattr(result, name)).resolve() != expected.resolve():
            raise ValueError(
                f"{name} should be {expected}, prep wrote {getattr(result, name)}."
            )
        if not _tiff_magic_ok(expected):
            raise ValueError(f"Refusing to cache a non-TIFF: {expected}")
    payload = {name: getattr(result, name) for name in _STORED}
    sidecar = folder / SIDECAR
    temporary = sidecar.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(sidecar)
    return replace(result, from_cache=False)


def discard_entry(cache_dir: Path, key: str) -> None:
    """Remove a half-built folder after a failed run."""
    folder = entry_dir(cache_dir, key)
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)


def clear_cache(cache_dir: Optional[Path] = None) -> int:
    """Delete every prep folder. Returns how many went.

    The README stays. So does any loose file that isn't a key folder.
    """
    directory = Path(cache_dir) if cache_dir else default_cache_dir()
    if not directory.is_dir():
        return 0
    removed = 0
    for child in directory.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
            removed += 1
    return removed
