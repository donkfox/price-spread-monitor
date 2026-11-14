from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .core import LicenseError, LicenseStore, to_timestamp, utc_now


DATABASE = Path(os.environ.get("LICENSE_DB", "license-data/licenses.sqlite3"))
store = LicenseStore(DATABASE)
app = FastAPI(title="Price Monitor License Server", docs_url=None, redoc_url=None)


class RedeemRequest(BaseModel):
    code: str = Field(min_length=8, max_length=64)
    device_id: str = Field(min_length=16, max_length=128)
    token: str = Field(default="", max_length=256)


class CheckRequest(BaseModel):
    device_id: str = Field(min_length=16, max_length=128)
    token: str = Field(min_length=16, max_length=256)


def response_payload(result, *, include_token: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": True,
        "license_id": result.license_id,
        "device_id": result.device_id,
        "expires_at": result.expires_at,
        "server_time": to_timestamp(utc_now()),
    }
    if include_token and result.token:
        payload["token"] = result.token
    return payload


@app.get("/health")
def health() -> dict[str, object]:
    return {"ok": True, "server_time": to_timestamp(utc_now())}


@app.post("/v1/activate")
def activate(request: RedeemRequest) -> dict[str, object]:
    try:
        result = store.redeem(request.code, request.device_id, request.token)
    except LicenseError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return response_payload(result, include_token=True)


@app.post("/v1/check")
def check(request: CheckRequest) -> dict[str, object]:
    try:
        result = store.check(request.device_id, request.token)
    except LicenseError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return response_payload(result)


