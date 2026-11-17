# Terrain prep cache (not in git)

`prepare_terrain()` writes one folder per prep key here:
`breached.tif`, `d8_pointer.tif`, `d8_accum_cells.tif`,
`streams.tif`, and `prep.json`. The key is a hash of the DEM's
bytes plus the prep params, so a repeat run reads these files
instead of starting Whitebox again.

Only this README is meant to be tracked. The folders are
gitignored. `terrain.clear_cache()` deletes them and leaves this
file.
