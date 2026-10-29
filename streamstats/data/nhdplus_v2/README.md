# NHDPlusV2 local data (not in git)

This folder holds the **NHDPlusV2 National Seamless Geodatabase
(Lower 48)** download used later for StreamStats map flowlines.

- Product: EPA/USGS NHDPlusV2 national seamless (File GDB inside a `.7z`)
- Typical download: ~7.3 GB compressed — **do not commit** the `.7z`
  or the extracted `.gdb`
- Fetch helper (Step 6): `python -m streamstats.fetch_nhdplus`

Only this README is meant to be tracked. Everything else under
`nhdplus_v2/` is gitignored.
