"""Story Runner backend.

Serves the demo page and a few safe, read-only API endpoints.
API tokens live only here (in .env), never in the browser.

SIM_MODE=true  -> every endpoint returns built-in demo data (no lab needed)
SIM_MODE=false -> endpoints call the real FortiGate using the token in .env
"""
import os
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

SIM_MODE = os.getenv("SIM_MODE", "true").lower() == "true"
FGT_HOST = os.getenv("FGT_HOST", "").strip()
FGT_TOKEN = os.getenv("FGT_TOKEN", "").strip()
# Lab FortiGates usually have self-signed certs, so verification is off unless you turn it on.
FGT_VERIFY_TLS = os.getenv("FGT_VERIFY_TLS", "false").lower() == "true"

app = FastAPI(title="Story Runner", docs_url=None, redoc_url=None)


async def fgt_get(path: str):
    """Read-only GET against the FortiGate REST API."""
    async with httpx.AsyncClient(verify=FGT_VERIFY_TLS, timeout=8) as client:
        r = await client.get(
            f"https://{FGT_HOST}{path}",
            headers={"Authorization": f"Bearer {FGT_TOKEN}"},
        )
        r.raise_for_status()
        return r.json()


@app.get("/api/health")
async def health():
    """What mode we're in and which devices are configured (never the tokens)."""
    live = not SIM_MODE and bool(FGT_HOST and FGT_TOKEN)
    return {
        "ok": True,
        "mode": "live" if live else "sim",
        "fortigate": FGT_HOST or None,
        # any folder under static/ with an index.html shows up as an available demo
        "apps": sorted(p.parent.name for p in Path("static").glob("*/index.html")),
    }


SIM_FABRIC = {
    "source": "sim",
    "switches": [
        {"name": "sw-core-01", "model": "FS-148F-FPOE", "status": "online"},
        {"name": "sw-idf-02", "model": "FS-124F-FPOE", "status": "online"},
        {"name": "sw-idf-03", "model": "FS-124F-POE", "status": "online"},
    ],
    "aps": [
        {"name": "ap-101", "model": "FAP-231G", "status": "online"},
        {"name": "ap-102", "model": "FAP-231G", "status": "online"},
        {"name": "ap-201", "model": "FAP-431G", "status": "online"},
    ],
}


@app.get("/api/fabric/status")
async def fabric_status():
    """Managed FortiSwitches and FortiAPs, as seen by the FortiGate."""
    if SIM_MODE or not (FGT_HOST and FGT_TOKEN):
        return SIM_FABRIC
    try:
        sw = await fgt_get("/api/v2/monitor/switch-controller/managed-switch/status")
        ap = await fgt_get("/api/v2/monitor/wifi/managed_ap")
        return {
            "source": "live",
            "switches": [
                {
                    "name": s.get("switch-id") or s.get("name"),
                    "model": s.get("os_version", "").split("-v")[0] or s.get("model"),
                    "status": s.get("status", "unknown"),
                }
                for s in sw.get("results", [])
            ],
            "aps": [
                {
                    "name": a.get("name") or a.get("wtp_id"),
                    "model": a.get("os_version", "").split("-v")[0],
                    "status": a.get("status", "unknown"),
                }
                for a in ap.get("results", [])
            ],
        }
    except Exception as e:  # show the presenter what broke instead of a blank screen
        return JSONResponse({"source": "error", "error": str(e)}, status_code=502)


# The page itself. Mounted last so /api/* routes win.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
