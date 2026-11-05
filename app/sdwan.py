"""SD-WAN demo: live WAN health from the FortiGate, and scenes that break/repair lab cables.

Safety rules:
- Only cables named in app/scenes/sdwan.json can be touched. The browser sends a scene id,
  never a cable id.
- Every break has a matching repair, and /api/sdwan/reset repairs every scene cable.
"""
import json
import random
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from . import lab

router = APIRouter(prefix="/api/sdwan")

SCENES_FILE = Path(__file__).parent / "scenes" / "sdwan.json"


def load_story():
    return json.loads(SCENES_FILE.read_text())


def scene_by_id(scene_id: str):
    for s in load_story()["scenes"]:
        if s["id"] == scene_id:
            return s
    raise HTTPException(404, f"No scene called {scene_id!r}")


def mode() -> str:
    return "live" if (lab.fs_ready() and lab.fgt_ready()) else "sim"


# --- Fabric Studio cable lookup ------------------------------------------

def _end_name(port):
    """Turn one end of a cable into 'Device:port'. Returns None if we can't tell."""
    if not isinstance(port, dict):
        return None
    dev = port.get("device")
    dev_name = dev.get("name") if isinstance(dev, dict) else port.get("dev_name")
    if not dev_name or not port.get("name"):
        return None
    return f"{dev_name}:{port['name']}"


async def live_cables():
    """Map 'Device:port' -> {id, state} for every cable in the running fabric."""
    body = await lab.fs_call(
        "GET",
        "/api/v1/runtime/cable",
        params=[
            ("related-fields", "port1"),
            ("related-fields", "port2"),
            ("related-fields", "port1.device"),
            ("related-fields", "port2.device"),
            ("related-fields", "state"),
        ],
    )
    cables = {}
    for c in body.get("object") or []:
        info = {"id": c["id"], "state": c.get("state")}
        for end in (c.get("port1"), c.get("port2")):
            name = _end_name(end)
            if name:
                cables[name] = info
    return cables


async def cable_for(spec: str):
    cables = await live_cables()
    if spec not in cables:
        known = ", ".join(sorted(cables)) or "none found"
        raise HTTPException(
            409, f"Can't find a running cable at {spec!r}. Cables I can see: {known}"
        )
    return cables[spec]


# --- Simulation (SIM_MODE) ------------------------------------------------
SIM = {"broken": set()}


def sim_health():
    story = load_story()
    hit_by = {}
    for s in story["scenes"]:
        if s["id"] in SIM["broken"]:
            hit_by.setdefault(s["member"], []).append(s)
    out = []
    for link in story["links"]:
        hit = hit_by.get(link["member"], [])
        down = bool(hit)
        out.append(
            {
                **link,
                "alive": not down,
                "link_up": not any(s["kind"] == "circuit" for s in hit),
                "latency": None if down else round(random.uniform(11, 16), 1),
                "jitter": None if down else round(random.uniform(0.2, 1.5), 2),
                "loss": 100.0 if down else 0.0,
            }
        )
    return out


# --- Endpoints -----------------------------------------------------------

@router.get("/story")
async def story():
    """The scenes and links, for drawing the page."""
    s = load_story()
    return {"mode": mode(), **s}


@router.get("/status")
async def status():
    """WAN health as the FortiGate sees it, plus which scene cables are currently broken."""
    story = load_story()
    if mode() == "sim":
        links = sim_health()
        return {"source": "sim", "at": time.time(), "links": links, "broken": sorted(SIM["broken"])}
    try:
        hc = await lab.fgt_get("/api/v2/monitor/virtual-wan/health-check")
        results = hc.get("results") or {}
        # results looks like {"<health check name>": {"port2": {...}, "port4": {...}}}
        members = {}
        for check in results.values():
            if isinstance(check, dict):
                for member, stats in check.items():
                    members.setdefault(member, stats)
        links = []
        for link in story["links"]:
            st = members.get(link["member"], {})
            alive = str(st.get("status", "")).lower() in ("up", "alive")
            links.append(
                {
                    **link,
                    "alive": alive,
                    "link_up": st.get("link") != "down" if st else None,
                    "latency": st.get("latency"),
                    "jitter": st.get("jitter"),
                    "loss": st.get("packet_loss"),
                    "raw_status": st.get("status"),
                }
            )
        cables = await live_cables()
        broken = [
            s["id"]
            for s in story["scenes"]
            if str(cables.get(s["cable"], {}).get("state", "")).lower() == "broken"
        ]
        return {"source": "live", "at": time.time(), "links": links, "broken": broken}
    except HTTPException:
        raise
    except Exception as e:  # show the presenter what broke instead of a blank screen
        return JSONResponse({"source": "error", "error": str(e)}, status_code=502)


async def _act(scene_id: str, action: str):
    scene = scene_by_id(scene_id)
    if mode() == "sim":
        (SIM["broken"].add if action == "break" else SIM["broken"].discard)(scene_id)
        return {"ok": True, "source": "sim", "scene": scene_id, "action": action}
    try:
        cable = await cable_for(scene["cable"])
        await lab.fs_call("POST", f"/api/v1/runtime/cable/{cable['id']}:{action}")
        return {"ok": True, "source": "live", "scene": scene_id, "action": action, "cable": cable["id"]}
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@router.post("/scene/{scene_id}/break")
async def break_scene(scene_id: str):
    return await _act(scene_id, "break")


@router.post("/scene/{scene_id}/repair")
async def repair_scene(scene_id: str):
    return await _act(scene_id, "repair")


@router.post("/reset")
async def reset():
    """Repair every cable the scenes know about. Safe to press any time."""
    story = load_story()
    if mode() == "sim":
        SIM["broken"].clear()
        return {"ok": True, "source": "sim", "repaired": [s["id"] for s in story["scenes"]]}
    repaired, errors = [], []
    try:
        cables = await live_cables()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    for s in story["scenes"]:
        cable = cables.get(s["cable"])
        if not cable:
            errors.append(f"{s['cable']}: not found")
            continue
        try:
            await lab.fs_call("POST", f"/api/v1/runtime/cable/{cable['id']}:repair")
            repaired.append(s["id"])
        except Exception as e:
            errors.append(f"{s['cable']}: {e}")
    return {"ok": not errors, "source": "live", "repaired": repaired, "errors": errors}


@router.get("/cables")
async def cables():
    """Debug helper: every running cable by name, so you can check the scene file matches."""
    if mode() == "sim":
        return {"source": "sim", "cables": {s["cable"]: {"id": None} for s in load_story()["scenes"]}}
    try:
        return {"source": "live", "cables": await live_cables()}
    except Exception as e:
        return JSONResponse({"source": "error", "error": str(e)}, status_code=502)
