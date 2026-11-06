"""SD-WAN demos: live WAN health from the FortiGate, and scenes that break/repair lab cables.

One scene file per demo (app/scenes/*.json), each served under its own prefix:
    /api/sdwan/...   Keep the Lights On   (scenes/sdwan.json, fabric Story-Fabric-SDWAN)
    /api/zones/...   Who Gets the Backup  (scenes/zones.json, fabric Story-Fabric-SDWAN_Zones)

Scene file shape (see the files for full examples):
    links:  the FortiGate's SD-WAN members  {member, label, nick, gateway, zone?}
    users:  lab hosts we SSH to             {id, name, label, env, sim_path?}   (default: Corp_User)
    scenes: what the presenter can break    {id, title, blurb, cuts: [{cable, member, kind}]}
            (an older single-cable form {cable, member, kind} is still accepted)

Safety rules:
- Only cables named in the scene file can be touched. The browser sends a scene id, never a
  cable id, and only a user id from the scene file, never a command.
- Every break has a matching repair, and /reset repairs every scene cable.
"""
import asyncio
import json
import random
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from . import lab
from .labusers import LabUser, LossTracker, sse

SCENES_DIR = Path(__file__).parent / "scenes"


def mode() -> str:
    return "live" if (lab.fs_ready() and lab.fgt_ready()) else "sim"


# --- Fabric Studio cable lookup (shared by every demo) ---------------------

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


def is_broken(info) -> bool:
    return str((info or {}).get("state", "")).lower() == "broken"


async def installed_fabrics():
    """Best effort: names of fabrics that currently have a runtime (are installed)."""
    try:
        body = await lab.fs_call("GET", "/api/v1/runtime/fabric")
        obj = body.get("object") or []
        obj = obj if isinstance(obj, list) else [obj]
        return [f.get("name") for f in obj if isinstance(f, dict) and f.get("name")]
    except Exception:
        return None


# --- One demo ---------------------------------------------------------------

def _normalize_scene(s):
    """Give every scene a list of cuts, whichever form the file used."""
    cuts = s.get("cuts") or [{"cable": s["cable"], "member": s["member"], "kind": s.get("kind", "circuit")}]
    out = {**s, "cuts": cuts}
    if len(cuts) == 1:  # the older page reads these directly
        out.setdefault("cable", cuts[0]["cable"])
        out.setdefault("member", cuts[0]["member"])
        out.setdefault("kind", cuts[0]["kind"])
    return out


DEFAULT_USERS = [{"id": "corp", "name": "Corp_User", "label": "Users", "env": "CORP"}]


def make_router(slug: str, scene_file: str) -> APIRouter:
    router = APIRouter(prefix=f"/api/{slug}")
    path = SCENES_DIR / scene_file
    sim = {"broken": set(), "install_started": 0.0}  # sim mode: broken cable names
    user_cache = {}  # user id -> {"ok", "at"}

    def load_story():
        s = json.loads(path.read_text())
        s["scenes"] = [_normalize_scene(x) for x in s.get("scenes", [])]
        s["users"] = s.get("users") or DEFAULT_USERS
        return s

    def scene_by_id(story, scene_id: str):
        for s in story["scenes"]:
            if s["id"] == scene_id:
                return s
        raise HTTPException(404, f"No scene called {scene_id!r}")

    def scene_cables(story):
        seen = []
        for s in story["scenes"]:
            for c in s["cuts"]:
                if c["cable"] not in seen:
                    seen.append(c["cable"])
        return seen

    def users(story):
        return {
            u["id"]: LabUser(u.get("env", u["id"]), u["name"], u.get("ssh_port"), u.get("ping_target"))
            for u in story["users"]
        }

    def user_or_404(story, user_id):
        all_users = users(story)
        uid = user_id or story["users"][0]["id"]
        if uid not in all_users:
            raise HTTPException(404, f"No user called {uid!r}")
        return uid, all_users[uid]

    def cut_state(story, broken_cables):
        """From the set of broken cable names: which scenes are fully broken, and which map
        segments ({member, kind}) are cut."""
        broken = [s["id"] for s in story["scenes"] if all(c["cable"] in broken_cables for c in s["cuts"])]
        cuts, seen = [], set()
        for s in story["scenes"]:
            for c in s["cuts"]:
                key = (c["member"], c["kind"])
                if c["cable"] in broken_cables and key not in seen:
                    seen.add(key)
                    cuts.append({"member": c["member"], "kind": c["kind"]})
        return broken, cuts

    # --- simulation ---
    def sim_health(story):
        _, cuts = cut_state(story, sim["broken"])
        hit = {}
        for c in cuts:
            hit.setdefault(c["member"], []).append(c["kind"])
        out = []
        for link in story["links"]:
            kinds = hit.get(link["member"], [])
            down = bool(kinds)
            base = link.get("sim_latency", 13.5)
            out.append({
                **link,
                "alive": not down,
                "link_up": "circuit" not in kinds,
                "latency": None if down else round(random.uniform(base * 0.85, base * 1.15), 1),
                "jitter": None if down else round(random.uniform(0.2, 1.5) * (base / 13.5) ** 0.5, 2),
                "loss": 100.0 if down else 0.0,
            })
        return out

    def sim_path(story, user_id):
        """Where a user's traffic would go in the simulation: the first live member it may use."""
        alive = {l["member"]: l["alive"] for l in sim_health(story)}
        user = next((u for u in story["users"] if u["id"] == user_id), {})
        order = user.get("sim_path") or [l["member"] for l in story["links"]]
        return next((m for m in order if alive.get(m)), None)

    def sim_latency(story, member):
        link = next((l for l in story["links"] if l["member"] == member), {})
        base = link.get("sim_latency", 13.5)
        return round(random.uniform(base * 0.9, base * 1.2) + 1.5, 1)

    async def _sim_ping(story, user_id):
        tracker = LossTracker()
        user = users(story)[user_id]
        yield sse({"type": "start", "target": user.target})
        seq, member, dead_ticks, quiet = 0, sim_path(story, user_id), 0, 0.0
        while True:
            await asyncio.sleep(1)
            alive = {l["member"]: l["alive"] for l in sim_health(story)}
            if member and alive.get(member):
                dead_ticks = 0
            else:
                dead_ticks += 1
                if dead_ticks >= 2:  # SD-WAN fails over after a couple of seconds
                    member, dead_ticks = sim_path(story, user_id), 0
            if member and alive.get(member):
                quiet = 0.0
                for ev in tracker.reply(seq, sim_latency(story, member)):
                    yield sse(ev)
            else:
                quiet += 1
                yield sse({"type": "silence", "seconds": quiet})
            seq += 1

    # --- endpoints ---
    @router.get("/story")
    async def story_ep():
        """The links, users and scenes, for drawing the page."""
        return {"mode": mode(), **load_story()}

    @router.get("/status")
    async def status():
        """WAN health as the FortiGate sees it, plus which scene cables are currently broken."""
        story = load_story()
        if mode() == "sim":
            broken, cuts = cut_state(story, sim["broken"])
            return {"source": "sim", "at": time.time(), "links": sim_health(story), "broken": broken, "cuts": cuts}
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
            broken_cables = {name for name in scene_cables(story) if is_broken(cables.get(name))}
            broken, cuts = cut_state(story, broken_cables)
            # The health-check API doesn't say whether the port itself is down, so work it out:
            # a broken "circuit" cable means the FortiGate's own WAN port lost carrier.
            port_down = {c["member"] for c in cuts if c["kind"] == "circuit"}
            links = []
            for link in story["links"]:
                st = members.get(link["member"], {})
                alive = str(st.get("status", "")).lower() in ("up", "alive")
                links.append({
                    **link,
                    "alive": alive,
                    "link_up": link["member"] not in port_down,
                    "latency": st.get("latency"),
                    "jitter": st.get("jitter"),
                    "loss": st.get("packet_loss"),
                    "raw_status": st.get("status"),
                })
            return {"source": "live", "at": time.time(), "links": links, "broken": broken, "cuts": cuts}
        except HTTPException:
            raise
        except Exception as e:  # show the presenter what broke instead of a blank screen
            return JSONResponse({"source": "error", "error": str(e)}, status_code=502)

    async def _act(scene_id: str, action: str):
        story = load_story()
        scene = scene_by_id(story, scene_id)
        names = [c["cable"] for c in scene["cuts"]]
        if mode() == "sim":
            for n in names:
                (sim["broken"].add if action == "break" else sim["broken"].discard)(n)
            return {"ok": True, "source": "sim", "scene": scene_id, "action": action}
        try:
            cables = await live_cables()
            missing = [n for n in names if n not in cables]
            if missing:
                known = ", ".join(sorted(cables)) or "none found"
                raise HTTPException(409, f"Can't find a running cable at {', '.join(missing)}. Cables I can see: {known}")
            done = []
            for n in names:
                # Skip cables already in the wanted state, so overlapping scenes don't trip each other.
                if is_broken(cables[n]) == (action == "break"):
                    continue
                await lab.fs_call("POST", f"/api/v1/runtime/cable/{cables[n]['id']}:{action}")
                done.append(n)
            return {"ok": True, "source": "live", "scene": scene_id, "action": action, "cables": done}
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
            sim["broken"].clear()
            return {"ok": True, "source": "sim", "repaired": scene_cables(story)}
        repaired, errors = [], []
        try:
            cables = await live_cables()
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
        for name in scene_cables(story):
            cable = cables.get(name)
            if not cable:
                errors.append(f"{name}: not found")
                continue
            try:
                await lab.fs_call("POST", f"/api/v1/runtime/cable/{cable['id']}:repair")
                repaired.append(name)
            except Exception as e:
                errors.append(f"{name}: {e}")
        return {"ok": not errors, "source": "live", "repaired": repaired, "errors": errors}

    @router.get("/cables")
    async def cables_ep():
        """Debug helper: every running cable by name, so you can check the scene file matches."""
        if mode() == "sim":
            return {"source": "sim", "cables": {n: {"id": None} for n in scene_cables(load_story())}}
        try:
            return {"source": "live", "cables": await live_cables()}
        except Exception as e:
            return JSONResponse({"source": "error", "error": str(e)}, status_code=502)

    # --- getting the lab ready ---
    async def _user_ok(uid, user):
        """Is this user answering SSH? Cached for a few seconds so polling doesn't hammer it."""
        c = user_cache.setdefault(uid, {"ok": None, "at": 0.0})
        if time.time() - c["at"] > 10:
            c["ok"] = await user.can_connect() if user.ready() else None
            c["at"] = time.time()
        return c["ok"]

    def _sim_lab(story):
        started = sim["install_started"]
        elapsed = time.time() - started if started else 999
        steps = [("fabric", "Fabric installed", 4), ("fortigate", "FortiGate answering", 10),
                 ("switch", "FortiSwitch online", 18)]
        steps += [(f"user_{u['id']}", f"{u['name']} reachable", 22 + i * 2) for i, u in enumerate(story["users"])]
        last = steps[-1][2]
        checks = [{"id": i, "label": l, "ok": elapsed >= t, "detail": "" if elapsed >= t else "waiting"}
                  for i, l, t in steps]
        return {"source": "sim", "fabric": story["fabric"], "ready": all(c["ok"] for c in checks),
                "installing": bool(started) and elapsed < last, "installed_fabrics": [story["fabric"]],
                "checks": checks}

    @router.get("/lab")
    async def lab_status():
        """Is this demo's fabric installed and every piece answering?"""
        story = load_story()
        if mode() == "sim":
            return _sim_lab(story)
        checks = []
        installed = await installed_fabrics()

        try:
            cables = await live_cables()
            missing = [n for n in scene_cables(story) if n not in cables]
            other = bool(installed) and story["fabric"] not in installed
            detail = ""
            if other:
                detail = f"running now: {', '.join(installed)}"
            elif missing:
                detail = f"missing cables: {', '.join(missing)}"
            checks.append({"id": "fabric", "label": "Fabric installed", "ok": not missing and not other,
                           "detail": detail})
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

        all_users = users(story)
        results = await asyncio.gather(*(_user_ok(uid, u) for uid, u in all_users.items()))
        for (uid, u), ok in zip(all_users.items(), results):
            checks.append({"id": f"user_{uid}", "label": f"{u.name} reachable", "ok": bool(ok),
                           "detail": "" if ok else ("not configured" if ok is None else "SSH not answering")})

        return {"source": "live", "fabric": story["fabric"], "ready": all(c["ok"] for c in checks),
                "installing": False, "installed_fabrics": installed, "checks": checks}

    @router.post("/lab/install")
    async def lab_install():
        """Install (or reinstall) the demo fabric. Every device boots from its snapshot, so this
        is also the full reset. It stops whatever fabric is running on the pod."""
        story = load_story()
        if mode() == "sim":
            sim["install_started"] = time.time()
            sim["broken"].clear()
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
        user_cache.clear()
        return {"ok": True, "source": "live", "fabric": story["fabric"], "fabric_id": fabric_id}

    # --- the users' view ---
    @router.get("/user/ping")
    async def user_ping(u: str | None = Query(None, description="user id from the scene file")):
        """Live ping from one lab user, as server-sent events. Stops when the page closes the stream."""
        story = load_story()
        uid, user = user_or_404(story, u)
        if mode() == "sim":
            gen = _sim_ping(story, uid)
        elif not user.ready():
            return JSONResponse({"error": f"{user.name} SSH isn't set up in .env ({user.prefix}_SSH_PASSWORD)"},
                                status_code=503)
        else:
            gen = user.ping_stream()
        return StreamingResponse(gen, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.get("/user/path")
    async def user_path(u: str | None = Query(None, description="user id from the scene file")):
        """Which WAN is this user's traffic taking right now (from traceroute hop 2)?"""
        story = load_story()
        uid, user = user_or_404(story, u)
        if mode() == "sim":
            member = sim_path(story, uid)
            via = next((l["gateway"] for l in story["links"] if l["member"] == member), None)
            return {"source": "sim", "user": uid, "member": member, "via": via}
        if not user.ready():
            return JSONResponse({"error": f"{user.name} SSH isn't set up in .env"}, status_code=503)
        try:
            return {"source": "live", "user": uid, **await user.path(story["links"])}
        except Exception as e:
            return JSONResponse({"error": f"Path check failed: {e}"}, status_code=502)

    return router


# The demos. Each gets its own endpoints, scene file and fabric.
routers = [
    make_router("sdwan", "sdwan.json"),  # Keep the Lights On
    make_router("zones", "zones.json"),  # Who Gets the Backup
]
