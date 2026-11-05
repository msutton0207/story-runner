"""SD-WAN demo: live WAN health from the FortiGate, and scenes that break/repair lab cables.

Safety rules:
- Only cables named in app/scenes/sdwan.json can be touched. The browser sends a scene id,
  never a cable id.
- Every break has a matching repair, and /api/sdwan/reset repairs every scene cable.
"""
import asyncio
import json
import random
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from . import corpuser, lab

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
SIM = {"broken": set(), "install_started": 0.0}


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
        cables = await live_cables()
        broken = [
            s["id"]
            for s in story["scenes"]
            if str(cables.get(s["cable"], {}).get("state", "")).lower() == "broken"
        ]
        # The health-check API doesn't say whether the port itself is down, so work it out:
        # a broken "circuit" scene cable means the FortiGate's own WAN port lost carrier.
        port_down = {s["member"] for s in story["scenes"] if s["id"] in broken and s["kind"] == "circuit"}
        links = []
        for link in story["links"]:
            st = members.get(link["member"], {})
            alive = str(st.get("status", "")).lower() in ("up", "alive")
            links.append(
                {
                    **link,
                    "alive": alive,
                    "link_up": link["member"] not in port_down,
                    "latency": st.get("latency"),
                    "jitter": st.get("jitter"),
                    "loss": st.get("packet_loss"),
                    "raw_status": st.get("status"),
                }
            )
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


# --- Getting the lab ready -------------------------------------------------
_corp_cache = {"ok": None, "at": 0.0}


async def _corp_ok():
    """Is Corp_User answering SSH? Cached for a few seconds so polling doesn't hammer it."""
    if time.time() - _corp_cache["at"] > 10:
        _corp_cache["ok"] = await corpuser.can_connect() if corpuser.ready() else None
        _corp_cache["at"] = time.time()
    return _corp_cache["ok"]


def _sim_lab():
    story = load_story()
    started = SIM["install_started"]
    elapsed = time.time() - started if started else 999
    steps = [("fabric", "Fabric installed", 4), ("fortigate", "FortiGate answering", 10),
             ("switch", "FortiSwitch online", 18), ("corp_user", "Corp_User reachable", 22)]
    checks = [{"id": i, "label": l, "ok": elapsed >= t, "detail": "" if elapsed >= t else "waiting"} for i, l, t in steps]
    return {"source": "sim", "fabric": story["fabric"], "ready": all(c["ok"] for c in checks),
            "installing": bool(started) and elapsed < 22, "installed_fabrics": [story["fabric"]], "checks": checks}


async def _installed_fabrics():
    """Best effort: names of fabrics that currently have a runtime (are installed)."""
    try:
        body = await lab.fs_call("GET", "/api/v1/runtime/fabric")
        obj = body.get("object") or []
        obj = obj if isinstance(obj, list) else [obj]
        return [f.get("name") for f in obj if isinstance(f, dict) and f.get("name")]
    except Exception:
        return None


@router.get("/lab")
async def lab_status():
    """Is the demo's fabric installed and every piece answering?"""
    if mode() == "sim":
        return _sim_lab()
    story = load_story()
    checks = []

    try:
        cables = await live_cables()
        missing = [s["cable"] for s in story["scenes"] if s["cable"] not in cables]
        checks.append({"id": "fabric", "label": "Fabric installed", "ok": not missing,
                       "detail": "" if not missing else f"missing cables: {', '.join(missing)}"})
    except Exception as e:
        checks.append({"id": "fabric", "label": "Fabric installed", "ok": False, "detail": str(e)})

    try:
        await lab.fgt_get("/api/v2/monitor/virtual-wan/health-check")
        checks.append({"id": "fortigate", "label": "FortiGate answering", "ok": True, "detail": ""})
    except Exception as e:
        checks.append({"id": "fortigate", "label": "FortiGate answering", "ok": False, "detail": str(e)[:120]})

    try:
        sw = await lab.fgt_get("/api/v2/monitor/switch-controller/managed-switch/status")
        states = [str(s.get("status", "")).lower() for s in sw.get("results", [])]
        ok = any(s in ("connected", "online", "up") for s in states)
        checks.append({"id": "switch", "label": "FortiSwitch online", "ok": ok,
                       "detail": "" if ok else (", ".join(states) or "no switch seen yet")})
    except Exception as e:
        checks.append({"id": "switch", "label": "FortiSwitch online", "ok": False, "detail": str(e)[:120]})

    corp = await _corp_ok()
    checks.append({"id": "corp_user", "label": "Corp_User reachable", "ok": bool(corp),
                   "detail": "" if corp else ("not configured" if corp is None else "SSH not answering")})

    installed = await _installed_fabrics()
    return {"source": "live", "fabric": story["fabric"], "ready": all(c["ok"] for c in checks),
            "installing": False, "installed_fabrics": installed, "checks": checks}


@router.post("/lab/install")
async def lab_install():
    """Install (or reinstall) the demo fabric. Every device boots from its snapshot, so this
    is also the full reset. It stops whatever fabric is running on the pod."""
    story = load_story()
    if mode() == "sim":
        SIM["install_started"] = time.time()
        SIM["broken"].clear()
        return {"ok": True, "source": "sim", "fabric": story["fabric"]}
    try:
        body = await lab.fs_call("GET", "/api/v1/model/fabric", params={"select": f'name="{story["fabric"]}"'})
        found = body.get("object") or []
        if not found:
            raise HTTPException(404, f"No fabric called {story['fabric']!r} on the pod")
        fabric_id = found[0]["id"]
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Couldn't look up the fabric: {e}"}, status_code=502)
    try:
        # Swagger: POST /api/v1/runtime/fabric/{fabric} clones the fabric into the runtime,
        # spawns and powers on every device. Any fabric already running is uninstalled first.
        await lab.fs_call(
            "POST",
            lab.FS_FABRIC_INSTALL_PATH.format(id=fabric_id),
            json={"power_on_vms": True, "install_vms": True, "install_vms_tasks": True},
            timeout=25,
        )
    except Exception as e:
        # The install runs as a background task on the pod; a slow reply doesn't mean it failed.
        if "timeout" not in type(e).__name__.lower() and "timed out" not in str(e).lower():
            return JSONResponse({"ok": False, "error": f"Install call failed: {e}"}, status_code=502)
    return {"ok": True, "source": "live", "fabric": story["fabric"], "fabric_id": fabric_id}


# --- The user's view (Corp_User) ---------------------------------------------

def _sim_path():
    links = sim_health()
    for link in links:
        if link["alive"]:
            return link["member"]
    return None


async def _sim_ping():
    tracker = corpuser.LossTracker()
    yield corpuser.sse({"type": "start", "target": corpuser.PING_TARGET})
    seq, member, dead_ticks, quiet = 0, _sim_path(), 0, 0.0
    while True:
        await asyncio.sleep(1)
        alive = {l["member"]: l["alive"] for l in sim_health()}
        if member and alive.get(member):
            dead_ticks = 0
        else:
            dead_ticks += 1
            if dead_ticks >= 2:  # SD-WAN fails over after a couple of seconds
                member, dead_ticks = _sim_path(), 0
        if member and alive.get(member):
            quiet = 0.0
            for ev in tracker.reply(seq, round(random.uniform(12, 16), 1)):
                yield corpuser.sse(ev)
        else:
            quiet += 1
            yield corpuser.sse({"type": "silence", "seconds": quiet})
        seq += 1


@router.get("/user/ping")
async def user_ping():
    """Live ping from Corp_User, as server-sent events. Stops when the page closes the stream."""
    if mode() == "sim":
        gen = _sim_ping()
    elif not corpuser.ready():
        return JSONResponse({"error": "Corp_User SSH isn't set up in .env (CORP_SSH_PASSWORD)"}, status_code=503)
    else:
        gen = corpuser.ping_stream()
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/user/path")
async def user_path():
    """Which WAN is Corp_User's traffic taking right now (from traceroute hop 2)?"""
    story = load_story()
    if mode() == "sim":
        member = _sim_path()
        via = next((l["gateway"] for l in story["links"] if l["member"] == member), None)
        return {"source": "sim", "member": member, "via": via}
    if not corpuser.ready():
        return JSONResponse({"error": "Corp_User SSH isn't set up in .env"}, status_code=503)
    try:
        return {"source": "live", **await corpuser.path(story["links"])}
    except Exception as e:
        return JSONResponse({"error": f"Path check failed: {e}"}, status_code=502)
