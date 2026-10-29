"""
Tributary Toolkit — Streamlit home.

Run from the data-downloader folder:
    streamlit run app.py

Pages under pages/ show up in the sidebar automatically
(Data Download, StreamStats, …).
"""

import sys
from pathlib import Path

# streamlit run app.py — keep project root on sys.path so
# `import data_sources` / `import gui` work from any page.
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st


def main() -> None:
    st.set_page_config(
        page_title="Tributary Toolkit",
        layout="wide",
    )

    st.title("Tributary Toolkit")
    st.write(
        "A small hub for stream restoration workflows. "
        "Pick a tool from the sidebar."
    )

    st.subheader("Tools")
    col_dd, col_ss = st.columns(2)
    with col_dd:
        st.markdown("### Data Download")
        st.write(
            "Discover monitoring sites (USGS, Aquarius, SNOTEL), "
            "inspect available series, and export CSV or SQLite. "
            "Map, draw-bbox, and shapefile search included."
        )
        st.caption("Open **Data Download** in the sidebar →")
    with col_ss:
        st.markdown("### StreamStats")
        st.write(
            "Snap a point to the stream network and delineate its "
            "watershed (map + GeoJSON download). Batch points, "
            "flowlines, and basin attributes come later."
        )
        st.caption("Open **StreamStats** in the sidebar →")

    st.divider()
    st.markdown(
        "Later tools (stubs not added yet): Terrain / REM, "
        "Habitat / HSI, storm analysis."
    )


if __name__ == "__main__":
    main()
