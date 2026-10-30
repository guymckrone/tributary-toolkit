"""Turn client exceptions into HTTP errors.

ValueError is my fault or the caller's (blank region, junk
basin) — 400. A requests failure is upstream — 502. I pass
the message through; the clients already wrote it.
"""

import requests
from fastapi import HTTPException


def http_error(exc: Exception) -> HTTPException:
    # Check this first. requests.JSONDecodeError is both a
    # ValueError and a RequestException, and a garbled USGS
    # reply is upstream's fault.
    if isinstance(exc, requests.RequestException):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(
        status_code=500,
        detail=f"{type(exc).__name__}: {exc}",
    )
