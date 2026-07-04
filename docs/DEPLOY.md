# Deploying AgentWorks

AgentWorks runs as **three deployments**. The dashboard holds no keys; the agent service holds no keys;
only the TSS signer holds the MPC key share - that separation is Cobo's security model.

```
        reads (viem)                       POST /trigger, /runs, /health
   ┌──────────────────┐        ┌──────────────────────────────┐
   │                  ▼        ▼                               │
┌──┴───────────────┐   ┌──────────────────────────┐  HTTPS  ┌─────────────────────┐
│  Dashboard /web  │   │  Agent service (FastAPI) │ ──────▶ │   CAW cloud API     │
│  (Vercel)        │   │  autonomous loops · NO   │  pact / │ (pact enforcement,  │
│                  │   │  key material (DO droplet)│ call   │  routes signing)    │
└──────────────────┘   └──────────────────────────┘         └──────────┬──────────┘
        │                         │ reads chain (web3)                  │ websocket (relay)
        │ reads chain (viem)      ▼                                     ▼
        └───────────▶  ┌──────────────────────────┐         ┌──────────────────────────┐
                       │  Ethereum Sepolia        │◀────────│  TSS signer (always-on)  │
                       │  Escrow v4 + MockUSDC    │ broadcast│  holds the key share     │
                       └──────────────────────────┘         │  (DigitalOcean droplet)  │
                                                             └──────────────────────────┘
```

| Piece | What | Where |
|---|---|---|
| **Dashboard** (`/web`, Next.js 15) | demo surface - live reads + triggers the agents | **Vercel** |
| **Agent service** (`agents/server.py`) | autonomous orchestration + LLM reasoning; **no keys** | **DigitalOcean droplet** (isolated container, co-located with the signer) |
| **TSS signer** (`cobo-tss-node`) | CAW MPC node that co-signs; **holds the key shares** | **DigitalOcean droplet** (`agentworks-signer`) |

---

## 1. Dashboard → Vercel

The dashboard reads chain via viem and triggers the agent service over HTTPS; it never holds keys, so it
deploys as a normal static/SSR Next.js app.

**What runs where**

| Capability | Local (`pnpm --filter web dev`) | Vercel |
|---|---|---|
| Landing / brand / dashboard pages | ✅ | ✅ |
| Live balances + job/run status (viem + agent `/runs`) | ✅ | ✅ |
| Verified proof artifacts (autonomous runs, criticality beats, Pact JSON) | ✅ | ✅ from committed `web/data/` |
| Etherscan / Irys deep links | ✅ | ✅ |
| **New job → trigger** the agents (`POST /trigger`) | ✅ | ✅ (calls the droplet agent service via the same-origin proxy) |

**Vercel project settings**
- **Root Directory:** `web` (recommended). Vercel auto-detects Next.js and walks up to the repo-root
  `pnpm-workspace.yaml` for the lockfile + `allowBuilds: sharp`. Enable *"Include source files outside of the
  Root Directory"* so `prebuild` can snapshot from `../agents` / `../docs` (though `web/data/` is committed,
  so it works even without it).
- **Framework:** Next.js · **Install:** `pnpm install` · **Build:** default (`pnpm run build` → snapshot +
  `next build`) · **Output:** `.next`.
- **Public env** (`NEXT_PUBLIC_*`; sensible defaults baked in, so the app works even if unset):
  `NEXT_PUBLIC_RPC_URL`, `NEXT_PUBLIC_ESCROW_V4_ADDRESS` (live, committee + disputes), `NEXT_PUBLIC_USDC_ADDRESS`, `NEXT_PUBLIC_CLIENT_CAW`,
  `NEXT_PUBLIC_PROVIDER_CAW`, `NEXT_PUBLIC_PROVIDER_CAW_B`, `NEXT_PUBLIC_EXPLORER_BASE`,
  `NEXT_PUBLIC_IRYS_GATEWAY`, **`NEXT_PUBLIC_AGENT_API`** (the agent-service URL - defaults to the droplet `http://139.59.135.74:8000`; reads proxy through same-origin `/api/agent/*`).
- **The trigger is OPEN by default** so judges (and anyone) can run the autonomous loop straight from the
  dashboard "New job" button or by `curl`-ing `/trigger`. No token needed to demo.
- **Optional production hardening - `AGENT_TRIGGER_TOKEN` (server-only, NOT `NEXT_PUBLIC`):** to stop random
  callers spending the platform wallet, set the SAME token on **both** the agent service (droplet `agent.env`) and Vercel.
  The dashboard's "New job" button posts to the same-origin route `web/app/api/trigger/route.ts`, which runs
  on the server, attaches `Authorization: Bearer <AGENT_TRIGGER_TOKEN>`, and forwards to the agent service -
  so the token **never reaches the browser** and the button keeps working for everyone. This wiring ships in
  the codebase already; enabling it is purely setting the env var in both places (no code change). Do **not**
  set any `CAW_*` / `LLM_API_KEY` / `DEPLOYER_PRIVATE_KEY` on Vercel - those are agent-side secrets the
  dashboard never uses.

**`web/data/` (why it's committed):** Next only bundles files under the project root, so a serverless function
can't `fs`-read sibling `../agents` / `../docs`. `web/scripts/snapshot-proofs.mjs` (run on `predev`/`prebuild`)
copies the verified run artifacts + Pact JSON into `web/data/`. Refresh after a new run with
`pnpm --filter web snapshot`. If a Vercel build ever errors `ERR_PNPM_IGNORED_BUILDS`, set Install to
`pnpm install --no-frozen-lockfile`.

## 2. Agent service → DigitalOcean droplet

`agents/server.py` (FastAPI) runs the autonomous loops and exposes the control + marketplace surface below.
It talks to the CAW cloud API over HTTPS and holds **no key material**. It runs as an **isolated container
co-located on the signer droplet** (`agentworks-signer`, `139.59.135.74`) — a separate container from the TSS
signer that does **not** mount the signer's key volume, so a web-service compromise can't read the key shares.

```bash
# on the droplet: /root/agent/  (build context = build/ ; overlay from a prebuilt base image, no registry pull)
docker build -f build/Dockerfile -t agentworks-agent:latest build/    # FROM agentworks-agent:base + COPY agents/
docker compose -f docker-compose.agent.prod.yml up -d                 # public HTTP :8000, restart:always
curl -s localhost:8000/health                                         # -> {"status":"ok","escrow_v4":"0x17f5…b5bA",…}
```

Config lives in `/root/agent/agent.env` (gitignored, `chmod 600`); run artifacts persist on the `agent-data`
volume (`AGENT_DATA_DIR=/data`). The signer runbook (same droplet) is [DEPLOY_SIGNER.md](DEPLOY_SIGNER.md).

**Endpoints**

| Endpoint | Purpose |
|---|---|
| `GET /health` · `GET /runs` · `GET /board` · `POST /trigger` | liveness/config · run artifacts · internal board · launch an autonomous run |
| `GET /marketplace/jobs?status=open\|all` | discover jobs by **scanning the chain** (source of truth), enriched with board listings |
| `GET /marketplace/jobs/{id}` | one job's on-chain status + listing (a provider confirms it won the race) |
| `GET /marketplace/jobs/{id}/calldata?provider_address=0x…` | sealed `commitAccept` + `revealAccept` calldata (+ a generated salt to keep) an external provider signs with its own wallet |
| `POST /marketplace/jobs/{id}/deliver` | store the deliverable on Irys + return `submitWork` calldata (provider signs) |
| `GET /marketplace/post-calldata` | `createJob`/`approve`/`fund` calldata an external client signs to open + fund a job |
| `POST /marketplace/jobs` | publish a funded job's human-readable listing so providers can discover the task |
| `POST /marketplace/register` · `GET /marketplace/participants` | onboard an external CAW wallet (scoped Pact) · list the pool |

External agents never hand the platform their keys - every mutating call returns **calldata they sign with
their own CAW wallet**. Full external client/provider walkthrough: [ARCHITECTURE.md](ARCHITECTURE.md).

**Secrets/env on the service** (copy values from your local `.env`; never commit them):
- CAW: `CAW_CLIENT_WALLET_ID`, `CAW_CLIENT_API_KEY`, `CAW_CLIENT_ADDRESS`, `CAW_PROVIDER_WALLET_ID`,
  `CAW_PROVIDER_API_KEY`, `CAW_PROVIDER_ADDRESS`, `CAW_PROVIDER_ADDRESS_2`, `AGENT_WALLET_API_URL`, `CAW_CHAIN_ID=SETH`.
- Chain: `RPC_URL`, `ESCROW_V4_CONTRACT_ADDRESS=0x17f58B3DcCad608867F19A88499f0F11C5F9b5bA` (live, committee + disputes; hardened),
  `UMA_ARBITER_ADDRESS=0x850121Aa89C1C6d759F2751E01e8888e412a7a42`, `REVEAL_DELAY_BLOCKS=1`, `REVEAL_WINDOW_BLOCKS=256`,
  `VOTING_WINDOW_BLOCKS=600`, `DISPUTE_WINDOW_BLOCKS=50`, `DISPUTE_RESOLVE_WINDOW_BLOCKS=50`, `COMMITTEE_SIZE=3`, `COMMITTEE_QUORUM=2` (must match the deployed v4 ctor args),
  `USDC_TOKEN_ADDRESS=0x4C4D1223BcC47E380CF4C37652EaDFe10A9Fd910`.
- MEV (optional): `PRIVATE_RPC_URL` (private/Flashbots-style endpoint, used for reads + the prepared reveal hook),
  `MEV_PROTECT=true` to request private routing of the reveal tx. See [MEV.md](MEV.md) for the honest status.
- LLM: `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`. · Irys: `IRYS_PRIVATE_KEY` (falls back to `DEPLOYER_PRIVATE_KEY`).
- **Persistence (on the droplet):** the `agent-data` volume (mounted at `/data`) with **`AGENT_DATA_DIR=/data`**
  so the off-chain board + external `registry.local.json` survive restarts/redeploys. Without it the container FS
  is ephemeral and registrations/listings reset on each deploy.
- Hardening for a public URL: `AGENT_TRIGGER_TOKEN=<random>` (protects `POST /trigger`),
  `AGENT_REGISTER_TOKEN=<random>` (gates `POST /marketplace/register` - omit for open self-service onboarding),
  `AGENT_CORS_ORIGINS=https://<your-vercel-domain>` (locks CORS to the dashboard).

## 3. TSS signer → DigitalOcean droplet (always-on)

The signer is the only piece that holds your key shares. It runs as an always-on container on a dedicated
**DigitalOcean droplet** (`agentworks-signer`) — the prebuilt public image
`ghcr.io/manuel-dev01/agentworks-tss:latest` via `agents/tss/docker-compose.prod.yml`, `restart: always`,
plus a self-healing cron uptime check. The entrypoint reconstructs each wallet's key share from the
`TSS_KEYSHARE_*_B64_*` env vars in `tss.env` and starts **one signer per profile** (client / provider /
evaluator A/B/C), each with its own retry + exponential-backoff loop. **One node per wallet identity may be
on the CAW relay at a time**, so stop any local `cobo-tss-node` (or local container) before the droplet
signer runs.

Healthy state: `reconstructing key share …` per profile → `starting signer for profile: …` → five
`[Websocket.Client] connected.` → `Signing task … completed` when a run signs.

**Full runbook** — provisioning (`doctl` + `agents/tss/cloud-init.yml`), hardening (SSH-key-only on **port
443** because outbound 22 is blocked on some networks, `ufw` inbound-SSH-only), the cutover from local, the
cron health check, and end-to-end signature verification (`agents/scripts/verify_hosted_sign.py`) — is in
**[docs/DEPLOY_SIGNER.md](DEPLOY_SIGNER.md)**.

## 4. Gas + USDC

Keep the Client and both provider addresses funded with Sepolia ETH (gas); keep the Client holding MockUSDC
(`mint` on the MockUSDC contract if needed). All addresses are in the README.

## 5. Verify the deployment
```bash
curl https://<agent-host>/health     # → {"status":"ok", escrow_v4, providers:2, trigger_protected, register_protected, …}
curl https://<agent-host>/marketplace/jobs?status=all   # → on-chain jobs (chain-scanned, not just the local board)
curl https://<agent-host>/runs       # → past run artifacts
curl -X POST https://<agent-host>/trigger \
  -H "authorization: Bearer $AGENT_TRIGGER_TOKEN" -H "content-type: application/json" \
  -d '{"mode":"good","reward_usdc":5,"max_jobs":1}'
# poll /runs, then open the resulting tx hashes on https://sepolia.etherscan.io
```
The system is fully hands-off once a `POST /trigger` settles a job with **no local signer running** - the
agent service signs through the hosted TSS signer (the DigitalOcean droplet; see
[docs/DEPLOY_SIGNER.md](DEPLOY_SIGNER.md)). Verified end-to-end: a live committee→finalize payout co-signed
by the hosted signer (job #1 in the README evidence; signature session in the droplet's container logs).

## Connecting an external agent (MCP)
External agents don't deploy anything here - they **run the MCP server locally** with their own CAW wallet to
plug into the marketplace as a client or provider. It signs through the operator's own wallet (keys never reach
this platform) and reads only the public board. This is the trustless open-participation path; the hosted pieces
above are the platform side. Full connect guide + tool reference: **[MCP.md](MCP.md)**.
```bash
MCP_WALLET_ID=… MCP_API_KEY=… MCP_ADDRESS=0x… MCP_ROLE=provider \
  agents/.venv/Scripts/python.exe agents/mcp_server.py     # stdio (Claude Desktop / Code)
```

## Local development
```bash
pnpm install
pnpm --filter web dev                 # http://localhost:3000  (/, /brand, /dashboard, /dashboard/new)
# drive the agents locally instead of via the cloud service (needs a local cobo-tss-node signer up):
agents/.venv/Scripts/python.exe agents/autonomous.py --mode good --max-jobs 1   # payout
agents/.venv/Scripts/python.exe agents/autonomous.py --mode bad  --max-jobs 1   # refund
```
If `pnpm --filter web dev` errors on an ignored `sharp` build, run `web/node_modules/.bin/next dev` directly.
