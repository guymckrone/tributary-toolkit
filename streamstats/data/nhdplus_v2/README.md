# NHDPlusV2 local data (not in git)

Optional later cache. The live map does not read this folder.
It aims with USGS hydrography tiles in the browser.

This folder is the place for an **NHDPlusV2 National Seamless
Geodatabase (Lower 48)** if that cache is built later.

- Product: EPA/USGS NHDPlusV2 national seamless (File GDB inside a `.7z`)
- Typical download: ~7.3 GB compressed — **do not commit** the `.7z`
  or the extracted `.gdb`
- There is no fetch helper in the repo yet

Only this README is meant to be tracked. Everything else under
`nhdplus_v2/` is gitignored.
