"""The user's view: run a few fixed commands on the lab's Corp_User host over SSH.

Only the commands in this file can run. The browser picks "ping" or "path"; it never sends
a command string. The SSH password lives in .env.
"""
import asyncio
import json
import os
import re

CORP_SSH_HOST = os.getenv("CORP_SSH_HOST", "").strip() or os.getenv("FS_HOST", "").strip()
CORP_SSH_PORT = int(os.getenv("CORP_SSH_PORT", "11002"))
CORP_SSH_USER = os.getenv("CORP_SSH_USER", "tc").strip()
CORP_SSH_PASSWORD = os.getenv("CORP_SSH_PASSWORD", "")
PING_TARGET = os.getenv("CORP_PING_TARGET", "1.1.1.1").strip()

# Fixed command list. Nothing else is ever sent to the host.
CMD_PING = f"ping {PING_TARGET}"
CMD_PATH = f"traceroute -n -m 2 -q 1 -w 1 {PING_TARGET}"

REPLY_RE = re.compile(r"seq=(\d+).*?time=([\d.]+)\s*ms")
HOP_RE = re.compile(r"^\s*(\d+)\s+(\d+\.\d+\.\d+\.\d+)")


def ready() -> bool:
    return bool(CORP_SSH_HOST and CORP_SSH_PASSWORD)


async def _connect():
    import asyncssh  # imported here so sim mode doesn't need it

    return await asyncssh.connect(
        CORP_SSH_HOST,
        port=CORP_SSH_PORT,
        username=CORP_SSH_USER,
        password=CORP_SSH_PASSWORD,
        known_hosts=None,  # lab box, rebuilt from snapshots: host key isn't stable
        connect_timeout=6,
    )


async def can_connect() -> bool:
    try:
        conn = await asyncio.wait_for(_connect(), timeout=8)
        conn.close()
        return True
    except Exception:
        return False


def sse(obj) -> str:
    return f"data: {json.dumps(obj)}\n\n"


class LossTracker:
    """Turns ping replies (and silences) into events: reply, silence, lost."""

    def __init__(self):
        self.last_seq = None
        self.sent = 0
        self.received = 0
        self.lost = 0

    def reply(self, seq: int, ms: float):
        events = []
        if self.last_seq is not None and seq > self.last_seq + 1:
            gap = seq - self.last_seq - 1
            self.lost += gap
            events.append({"type": "lost", "count": gap, "from": self.last_seq + 1, "to": seq - 1})
        self.sent = seq + 1
        self.received += 1
        self.last_seq = seq
        events.append({"type": "reply", "seq": seq, "ms": ms})
        events.append({"type": "stats", "sent": self.sent, "received": self.received, "lost": self.lost})
        return events


async def ping_stream():
    """Yield server-sent events for a live ping from Corp_User until the browser disconnects."""
    tracker = LossTracker()
    proc = None
    try:
        conn = await _connect()
    except Exception as e:
        yield sse({"type": "error", "error": f"Couldn't SSH to Corp_User: {e}"})
        return
    try:
        proc = await conn.create_process(CMD_PING)
        yield sse({"type": "start", "target": PING_TARGET})
        quiet = 0.0
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.25)
            except asyncio.TimeoutError:
                quiet += 1.25
                yield sse({"type": "silence", "seconds": round(quiet, 1)})
                continue
            if not line:  # ping ended
                yield sse({"type": "end"})
                break
            m = REPLY_RE.search(line)
            if m:
                quiet = 0.0
                for ev in tracker.reply(int(m.group(1)), float(m.group(2))):
                    yield sse(ev)
    finally:
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        conn.close()


def parse_path(output: str, links):
    """Find hop 2 (the ISP router) in traceroute output and match it to a WAN link."""
    hops = {}
    for line in output.splitlines():
        m = HOP_RE.match(line)
        if m:
            hops[int(m.group(1))] = m.group(2)
    hop2 = hops.get(2)
    for link in links:
        if hop2 and hop2 == link.get("gateway"):
            return {"member": link["member"], "via": hop2, "hops": hops}
    return {"member": None, "via": hop2, "hops": hops}


async def path(links):
    """Which WAN is Corp_User's traffic actually using right now?"""
    conn = await asyncio.wait_for(_connect(), timeout=8)
    try:
        r = await asyncio.wait_for(conn.run(CMD_PATH, check=False), timeout=10)
        return parse_path((r.stdout or "") + (r.stderr or ""), links)
    finally:
        conn.close()
