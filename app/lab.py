"""Connections to the lab: the FortiGate REST API and the Fabric Studio API.

Everything secret (tokens, credentials) is read from .env here and never leaves the server.
"""
import os
import time

import httpx

SIM_MODE = os.getenv("SIM_MODE", "true").lower() == "true"

# --- FortiGate -------------------------------------------------------------
FGT_HOST = os.getenv("FGT_HOST", "").strip()
FGT_TOKEN = os.getenv("FGT_TOKEN", "").strip()
# Lab FortiGates usually have self-signed certs, so verification is off unless you turn it on.
FGT_VERIFY_TLS = os.getenv("FGT_VERIFY_TLS", "false").lower() == "true"


def fgt_ready() -> bool:
    return not SIM_MODE and bool(FGT_HOST and FGT_TOKEN)


async def fgt_get(path: str):
    """Read-only GET against the FortiGate REST API."""
    async with httpx.AsyncClient(verify=FGT_VERIFY_TLS, timeout=8) as client:
        r = await client.get(
            f"https://{FGT_HOST}{path}",
            headers={"Authorization": f"Bearer {FGT_TOKEN}"},
        )
        r.raise_for_status()
        return r.json()


# --- Fabric Studio ---------------------------------------------------------
# FS_HOST: the Fabric Studio pod address (e.g. 10.50.50.107)
# FS_CREDENTIAL: from the Fabric Studio CLI:  system oauth2 application default create story-runner
#   (it prints one base64 value = client_id:client_secret)
FS_HOST = os.getenv("FS_HOST", "").strip()
FS_CREDENTIAL = os.getenv("FS_CREDENTIAL", "").strip()
FS_VERIFY_TLS = os.getenv("FS_VERIFY_TLS", "false").lower() == "true"

_fs_token = {"value": "", "expires": 0.0}


def fs_ready() -> bool:
    return not SIM_MODE and bool(FS_HOST and FS_CREDENTIAL)


async def _fs_new_token(client: httpx.AsyncClient) -> str:
    """OAuth2 client-credentials grant. Tokens last about 10 hours; we renew a bit early."""
    r = await client.post(
        f"https://{FS_HOST}/oauth2/token/",
        headers={"Authorization": f"Basic {FS_CREDENTIAL}", "Cache-Control": "no-cache"},
        data={"grant_type": "client_credentials"},
    )
    r.raise_for_status()
    body = r.json()
    _fs_token["value"] = body["access_token"]
    _fs_token["expires"] = time.time() + int(body.get("expires_in", 3600)) - 300
    return _fs_token["value"]


async def fs_call(method: str, path: str, params=None):
    """Call the Fabric Studio API with a bearer token (no cookies, no CSRF needed).

    Gets a token on first use, and once more if the server says the token is no good.
    """
    async with httpx.AsyncClient(verify=FS_VERIFY_TLS, timeout=15) as client:
        for attempt in (1, 2):
            token = _fs_token["value"]
            if not token or time.time() > _fs_token["expires"] or attempt == 2:
                token = await _fs_new_token(client)
            r = await client.request(
                method,
                f"https://{FS_HOST}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code in (401, 403) and attempt == 1:
                continue
            r.raise_for_status()
            body = r.json()
            if body.get("status") == "error":
                raise RuntimeError(f"Fabric Studio said: {body.get('errors')}")
            return body
