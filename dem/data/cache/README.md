# DEM cache (not in git)

`get_dem()` writes finished GeoTIFFs and a JSON sidecar here.
Repeat requests for the same box, resolution, and provider
read these files instead of calling USGS again.

Only this README is meant to be tracked. `*.tif` and `*.json`
are gitignored. `clear_cache()` deletes the rasters and leaves
this file.
