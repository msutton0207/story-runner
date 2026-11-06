"""The users' view: run a few fixed commands on the lab's user hosts (Corp_User, Guest_User) over SSH.

Only the commands in this file can run. The browser picks a user and "ping" or "path"; it never
sends a command string. SSH settings come from .env, one block per user, keyed by the user's
"env" prefix in the scene file:

    CORP_SSH_HOST / _PORT / _USER / _PASSWORD      (Corp_User, port 11002 by default)
    GUEST_SSH_HOST / _PORT / _USER / _PASSWORD     (Guest_User, port 11003 by default)

Anything a GUEST_ setting leaves out falls back to the CORP_ one (the users are cloned from the
same snapshot, so they share a login), and the host falls back to FS_HOST.
"""
import asyncio
import json
import os
import re

REPLY_RE = re.compile(r"seq=(\d+).*?time=([\d.]+)\s*ms")
HOP_RE = re.compile(r"^\s*(\d+)\s+(\d+\.\d+\.\d+\.\d+)")

DEFAULT_PORTS = {"CORP": 11002, "GUEST": 11003}


def _env(prefix: str, key: str, fallback: str = "") -> str:
    v = os.getenv(f"{prefix}_{key}", "").strip()
    if v:
        return v
    if prefix != "CORP":
        v = os.getenv(f"CORP_{key}", "").strip()
        if v:
            return v
    return fallback


class LabUser:
    """One lab host we can SSH to and run the two fixed commands on."""

    def __init__(self, prefix: str, name: str, ssh_port: int | None = None, ping_target: str | None = None):
        self.prefix = prefix.upper()
        self.name = name
        self.host = _env(self.prefix, "SSH_HOST") or os.getenv("FS_HOST", "").strip()
        port = os.getenv(f"{self.prefix}_SSH_PORT", "").strip()
        self.port = int(port) if port else int(ssh_port or DEFAULT_PORTS.get(self.prefix, 22))
        self.user = _env(self.prefix, "SSH_USER", "tc")
        # Passwords are read raw (not stripped) so a password with spaces still works.
        self.password = os.getenv(f"{self.prefix}_SSH_PASSWORD", "") or os.getenv("CORP_SSH_PASSWORD", "")
        self.target = (ping_target or _env(self.prefix, "PING_TARGET", "1.1.1.1")).strip()
        # Fixed command list. Nothing else is ever sent to the host.
        self.cmd_ping = f"ping {self.target}"
        self.cmd_path = f"traceroute -n -m 2 -q 1 -w 1 {self.target}"

    def ready(self) -> bool:
        return bool(self.host and self.password)

    async def _connect(self):
        import asyncssh  # imported here so sim mode doesn't need it

        return await asyncssh.connect(
            self.host,
            port=self.port,
            username=self.user,
            password=self.password,
            known_hosts=None,  # lab box, rebuilt from snapshots: host key isn't stable
            connect_timeout=6,
        )

    async def can_connect(self) -> bool:
        try:
            conn = await asyncio.wait_for(self._connect(), timeout=8)
            conn.close()
            return True
        except Exception:
            return False

    async def ping_stream(self):
        """Yield server-sent events for a live ping until the browser disconnects."""
        tracker = LossTracker()
        proc = None
        try:
            conn = await self._connect()
        except Exception as e:
            yield sse({"type": "error", "error": f"Couldn't SSH to {self.name}: {e}"})
            return
        try:
            proc = await conn.create_process(self.cmd_ping)
            yield sse({"type": "start", "target": self.target})
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

    async def path(self, links):
        """Which WAN is this user's traffic actually using right now?"""
        conn = await asyncio.wait_for(self._connect(), timeout=8)
        try:
            r = await asyncio.wait_for(conn.run(self.cmd_path, check=False), timeout=10)
            return parse_path((r.stdout or "") + (r.stderr or ""), links)
        finally:
            conn.close()


def sse(obj) -> str:
    return f"data: {json.dumps(obj)}\n\n"


class LossTracker:
    """Turns ping replies into events: reply, lost (a gap in sequence numbers), stats."""

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


def parse_path(output: str, links):
    """Find hop 2 (the ISP router) in traceroute output and match it to a WAN link's gateway."""
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
