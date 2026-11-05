# Story Runner

Story-driven demo apps for a Fortinet SE covering public sector (higher ed and local
government in Kansas and Missouri). The demos run on a lab VM and talk to real lab gear
(FortiGate, FortiSwitch, FortiAP, FortiAnalyzer, FortiAuthenticator, FortiTester) so the
presenter talks and clicks instead of typing. The owner steers the code but isn't a
full-time developer: explain changes plainly and give exact commands to run.

## Layout

    docker-compose.yml        two services: story-runner (port 80) and ai-basics (port 3100)
    Dockerfile, app/main.py   Python FastAPI backend: serves static/ and the /api/* endpoints
    static/index.html         "Demo Lab" landing page, one card per demo
    static/two-fridays/       Two Fridays demo (single HTML file)
    ai-basics/                AI Basics Lab: zero-dependency Node server + public/story.html
                              (story mode, default) and public/index.html (tabbed lab at /lab)
    update.sh                 run on the VM: git pull + rebuild + restart
    .env.example              template for .env (the real .env lives only on the VM)

## Deploying

The repo is the source of truth. Push to `main`, then on the VM:

    ~/story-runner/update.sh

The VM is Ubuntu Server 24.04 on Proxmox, Docker + compose, static IP on the lab
management VLAN. Containers use `restart: unless-stopped`. Anyone editing on the VM
must commit and push, or the next pull will conflict.

## Rules

- Never commit `.env` or any API key or token. `.gitignore` covers `.env` and `**/.env`.
- Tokens stay server-side. The browser only calls this app's own `/api/*` routes.
- Lab API calls are read-only unless a demo explicitly needs a change, and every
  change needs a matching reset.
- No customer names, discovery notes or real lab credentials in the repo.
- `SIM_MODE=true` (the default) serves built-in demo data; anything simulated is
  labelled "Simulated" on screen. Never present simulated numbers as real.
- The Two Fridays "usual way" minutes are illustrative defaults the presenter edits.
- Lab FortiGates use self-signed certs, so `FGT_VERIFY_TLS` defaults to false.

## The demos

**Two Fridays** (`static/two-fridays/`): one lean IT shop's Friday before a long weekend,
told twice (the usual way vs. the Fortinet Fabric) on two diverging clocks. Persona
toggle: City/County (Dana, City of Cedar Bend) or Higher Ed (Marcus, Flint Hills
University). Stops: coffee report (FAZ daily summary with a "spike day" toggle), new AP,
new network/VLAN, "the network is slow", report for Monday, 4:47 PM quarantine, closing
timeline. Comic-strip style in Fortinet red and ink (Fredoka/Nunito). The 4:47 stop sets
up a ransomware sequel. Each stop lists the FortiOS/FAZ API calls that would make it live.

**AI Basics Lab** (`ai-basics/`): how an LLM really works, for a networking/security
audience. Story mode has about 35 stops across AI 101, Risks and AI 201; it opens with
tokens, then hidden rules (system prompt). The server pins the model and caps
`max_tokens` at 1024. The tokenizer and embedding demos load from CDNs in the browser.
See `ai-basics/README.md` for the run of show.

**Two Circuits, One Bundle** (`static/sdwan/`, backend `app/sdwan.py`): SD-WAN with
broadband (WAN1, port2) and cellular (WAN2, port4) on the Fabric Studio fabric
`Story-FGT-SDWAN`. Live WAN health from the FortiGate's
`/api/v2/monitor/virtual-wan/health-check`; scenes break and repair lab cables through the
Fabric Studio API (`/api/v1/runtime/cable/{id}:break` / `:repair`, OAuth2 client
credentials in `FS_CREDENTIAL`). Scenes live in `app/scenes/sdwan.json` and name cables as
`Device:port`; only those cables can be touched, and "Repair everything" resets them all.
`GET /api/sdwan/cables` lists every running cable by name for checking the scene file.

## Adding a demo

1. Put it in its own folder under `static/` (or its own container for a separate server).
2. Add a card to `static/index.html`.
3. Keep the comic style and palette, label simulated data, and add a reset if it
   changes lab state.

## Checks before pushing

- `python3 -m py_compile app/main.py`
- Extract each page's `<script>` and run `node --check` on it.
- `node --check ai-basics/server.js`

## Open to-dos

- Read-only REST API admin on the FortiGate; token into `.env`; set `SIM_MODE=false`.
- Wire Two Fridays stop 1 to real FortiAnalyzer report data, and confirm the FAZ
  template name ("Daily Summary").
- Check `/api/fabric/status` field names against a real FortiGate response.
- "Watch mode": poll the lab and react when the presenter acts in the real GUI.
- "Open in FortiGate" buttons that jump to the right GUI page for each stop.
- Discovery capture: notes on each stop plus a copyable summary for the account manager.
- Local DNS name and TLS (Caddy; Let's Encrypt via DNS challenge if a domain is owned).
- Proxmox host is on PVE 8 (end of support); plan the upgrade to 9.
