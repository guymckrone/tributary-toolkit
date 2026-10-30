"""CSV / SQLite / ZIP exports for a download.

Same shapes as the Streamlit download page. I don't import
gui/download.py — that module pulls in Streamlit.
"""

import io
import os
import re
import sqlite3
import tempfile
import zipfile
from typing import List

import pandas as pd

# DataPoint.to_dict() keys. to_dataframe([]) returns a frame
# with no columns, and SQLite can't create a table with none.
_COLUMNS = [
    "timestamp", "value", "location", "parameter",
    "unit", "source", "quality_flag",
]


def _with_columns(df: pd.DataFrame) -> pd.DataFrame:
    if len(df.columns) == 0:
        return pd.DataFrame(columns=_COLUMNS)
    return df


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return _with_columns(df).to_csv(index=False).encode("utf-8")


def dataframe_to_sqlite_bytes(
    df: pd.DataFrame,
    table_name: str = "observations",
) -> bytes:
    """One SQLite file in memory. Timestamps stored as text."""
    out = _with_columns(df).copy()
    if "timestamp" in out.columns:
        out["timestamp"] = out["timestamp"].astype(str)

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        try:
            out.to_sql(table_name, conn, index=False, if_exists="replace")
            conn.commit()
        finally:
            conn.close()
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def safe_stem(text: str, max_len: int = 80) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", str(text).strip())
    cleaned = cleaned.strip("._") or "series"
    return cleaned[:max_len]


def unique_stems(stems: List[str]) -> List[str]:
    seen: dict = {}
    unique: List[str] = []
    for stem in stems:
        count = seen.get(stem, 0) + 1
        seen[stem] = count
        unique.append(stem if count == 1 else f"{stem}_{count}")
    return unique


def parts_to_zip(parts: List[dict], fmt: str) -> bytes:
    """fmt is 'csv' or 'db'. One file per part inside the zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for part in parts:
            stem = part["stem"]
            part_df: pd.DataFrame = part["df"]
            if fmt == "csv":
                zf.writestr(f"{stem}.csv", dataframe_to_csv_bytes(part_df))
            elif fmt == "db":
                zf.writestr(f"{stem}.db", dataframe_to_sqlite_bytes(part_df))
            else:
                raise ValueError(f"Unknown zip format: {fmt!r}")
    return buf.getvalue()
