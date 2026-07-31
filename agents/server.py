"""FastAPI control surface for the open-marketplace agent service.

The dashboard (Vercel) reads this over the same-origin proxy; the service talks to the CAW cloud API over
HTTPS. On-chain SIGNING happens via the TSS node connected to the CAW relay (on a host the operator controls,
see docs/DEPLOY_SIGNER.md). This process holds NO key material.

At startup it launches the platform's own agents as STANDING participants (provider + committee that claim +
vote on any open job) and a neutral settlement watcher (permissionless finalize). It never auto-posts jobs —
clients post with their own wallet via MCP `post_job` / the calldata rail.

Endpoints:
  GET  /health                     liveness + config + the participant pool + service status
  GET  /board                      current open-job listings (off-chain marketplace listing)
  GET  /runs, /runs/{job_id}       run artifacts written by the standing agents, newest first
  GET  /marketplace/directory      the ONE canonical participant list (platform + registered)
  GET  /marketplace/participants   same pool, legacy shape
  *    /marketplace/*              open-marketplace calldata + registration rail (non-custodial)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import autonomous
import config
import registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("server")

app = FastAPI(title="AgentWorks autonomous agent service", version="6.5")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.environ.get("AGENT_CORS_ORIGINS", "*").split(",") if o],
    allow_methods=["*"], allow_headers=["*"],
)

_REGISTER_TOKEN = os.environ.get("AGENT_REGISTER_TOKEN", "")  # if set, /marketplace/register* requires it
# Gate for the committee-verdict service. VERDICT_TOKEN is the current name; ACP_VERDICT_TOKEN is honored as a
# legacy fallback so the extracted Virtuals adapter's existing env keeps working during the transition.
_VERDICT_TOKEN = os.environ.get("VERDICT_TOKEN", "") or os.environ.get("ACP_VERDICT_TOKEN", "")

# Bounds on caller-supplied text so an anonymous request can't bloat the board / platform Irys account.
_MAX_TASK_LEN = 2000
_MAX_CRITERIA_LEN = 2000
_MAX_DELIVERABLE_LEN = 100_000  # 100 KB cap on platform-funded Irys uploads


def _require_token(authorization: str, token: str) -> None:
    """Bearer-token gate. Open when `token` is empty; else require `Authorization: Bearer <token>`
    (constant-time compare so the token can't be recovered via response timing)."""
    if token and not secrets.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(status_code=401, detail="missing/invalid bearer token")


# The canonical job spec-hash lives in escrow_v4.spec_hash(task, criteria, salt) — salt-bound and
# id-INDEPENDENT, so a listing can be re-derived + bound from the real createJob receipt id (no id race).


def _seed_data_dir() -> None:
    """First-boot seed: when AGENT_DATA_DIR points at a fresh volume, copy the committed run artifacts +
    board so `/runs` and the board aren't empty after a redeploy. Idempotent - never overwrites volume data."""
    if not os.environ.get("AGENT_DATA_DIR"):
        return
    src = Path(__file__).resolve().parent / "scripts" / ".market"
    dst = autonomous.MARKET_DIR
    if src.resolve() == dst.resolve():
        return
    try:
        (dst / "runs").mkdir(parents=True, exist_ok=True)
        copied = 0
        for p in (src / "runs").glob("*.json"):
            target = dst / "runs" / p.name
            if not target.exists():
                shutil.copy2(p, target)
                copied += 1
        if (src / "board.json").exists() and not (dst / "board.json").exists():
            shutil.copy2(src / "board.json", dst / "board.json")
        log.info("[seed] data dir %s seeded (%d run artifacts copied)", dst, copied)
    except Exception as e:  # never block startup on a seed failure
        log.warning("[seed] data-dir seed skipped: %s", e)


_seed_data_dir()


# ── standing platform services ──
# The platform runs its own agents as STANDING participants (provider + committee claim + vote on any open
# job) and a neutral settlement watcher (permissionless finalize). It NEVER auto-posts jobs — clients post
# with their own wallet (MCP `post_job` / the calldata rail). There is no demo trigger.
#
# REAL-MONEY SAFETY: the standing agents spend the platform wallet. On a real-money chain that must not happen
# just because the container booted. When AGENT_ARM_TOKEN is set the spend path boots DISARMED — the agents +
# watcher launch only after an explicit, bearer-gated `POST /arm`. With no arm token (testnet/dev) the legacy
# behaviour is preserved: honor PLATFORM_AGENTS / SETTLEMENT_WATCHER and auto-start at boot.
_PLATFORM_AGENTS = os.environ.get("PLATFORM_AGENTS", "1") != "0"
_WATCHER_ENABLED = os.environ.get("SETTLEMENT_WATCHER", "1") != "0"
_ARM_TOKEN = os.environ.get("AGENT_ARM_TOKEN", "")  # if set, boot DISARMED; arm via POST /arm with this bearer
_stop = asyncio.Event()
_bg_tasks: list[asyncio.Task] = []
_armed = False


def _services_active() -> bool:
    return any(not t.done() for t in _bg_tasks)


def _launch_services() -> dict:
    """Launch the standing platform agents + settlement watcher if enabled and not already running.
    Idempotent — a second call while active is a no-op. This is the ONE place the spend path is armed."""
    global _armed
    if _services_active():
        return {"armed": _armed, "already_active": True, "launched": []}
    _bg_tasks[:] = [t for t in _bg_tasks if not t.done()]  # drop finished tasks from a prior disarm
    _stop.clear()
    launched: list[str] = []
    if _PLATFORM_AGENTS:
        _bg_tasks.append(asyncio.create_task(autonomous.platform_agents(_stop)))
        launched.append("platform_agents")
        log.info("[server] standing platform agents launched")
    if _WATCHER_ENABLED and autonomous.has_first_party_signer():
        _bg_tasks.append(asyncio.create_task(autonomous.settlement_watcher(stop=_stop)))
        launched.append("settlement_watcher")
        log.info("[server] settlement watcher launched")
    _armed = True
    return {"armed": _armed, "already_active": False, "launched": launched}


@app.on_event("startup")
async def _start_services() -> None:
    if _ARM_TOKEN:
        log.info("[server] AGENT_ARM_TOKEN set — spend path booted DISARMED; POST /arm to launch agents")
        return
    if not (_PLATFORM_AGENTS or _WATCHER_ENABLED):
        log.info("[server] platform agents + watcher both disabled")
        return
    log.info("[server] no arm token — auto-starting: %s", _launch_services().get("launched"))


@app.on_event("shutdown")
async def _stop_services() -> None:
    _stop.set()
    for t in _bg_tasks:
        if not t.done():
            t.cancel()


@app.post("/arm")
async def arm(authorization: str = Header(default="")) -> dict:
    """Arm the spend path: launch the standing platform agents + settlement watcher. Gated by AGENT_ARM_TOKEN
    when set. The real-money go switch — a funded mainnet container stays idle until this is called.
    Must be async: _launch_services() calls asyncio.create_task, which needs the running event loop."""
    _require_token(authorization, _ARM_TOKEN)
    return _launch_services()


@app.post("/disarm")
async def disarm(authorization: str = Header(default="")) -> dict:
    """Disarm the spend path: stop the standing agents + watcher. In-flight settlements already broadcast are
    unaffected; no NEW spend is initiated once disarmed. Gated by AGENT_ARM_TOKEN when set."""
    _require_token(authorization, _ARM_TOKEN)
    global _armed
    _stop.set()
    for t in _bg_tasks:
        if not t.done():
            t.cancel()
    _armed = False
    return {"armed": False, "stopped": True}


def _artifacts() -> list[dict]:
    out = []
    if autonomous.RUNS_DIR.exists():
        for p in autonomous.RUNS_DIR.glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
    out.sort(key=lambda r: r.get("job_id", 0), reverse=True)
    return out


@app.get("/health")
def health() -> dict:
    pool = registry.load_pool()
    return {
        "status": "ok",
        "chain_id": config.CHAIN_ID,
        "escrow_v4": config.ESCROW_V4_ADDRESS,
        "usdc": config.USDC_ADDRESS,
        "participants": [p.public() for p in pool],
        "providers": len(registry.providers(pool)),
        "evaluators": len(registry.evaluators()),
        "register_protected": bool(_REGISTER_TOKEN),
        "platform_agents": _PLATFORM_AGENTS,
        "watcher": {"enabled": _WATCHER_ENABLED, "active": _services_active()},
        "arm_protected": bool(_ARM_TOKEN),  # real-money posture: spend path requires an explicit POST /arm
        "armed": _armed,                    # True once the standing agents/watcher are running
    }


@app.get("/board")
def board() -> dict:
    return autonomous._read_board()


@app.get("/runs")
def runs() -> list[dict]:
    return _artifacts()


@app.get("/runs/{job_id}")
def run_one(job_id: int) -> dict:
    for r in _artifacts():
        if r.get("job_id") == job_id:
            return r
    raise HTTPException(status_code=404, detail=f"no run artifact for job {job_id}")


# ── Open Marketplace API ────────────────────────────────────────────────────
# These endpoints turn the internal orchestrator into a public marketplace.
# External agents bring their own CAW wallet, register, discover jobs (the chain
# is the source of truth), fund/post, accept, and deliver. The platform holds NO
# external keys - it returns calldata the agent signs with its own CAW wallet.

_ZERO = "0x0000000000000000000000000000000000000000"


def _listing_view(job_id: int, on_chain: dict, listing: dict | None) -> dict:
    """Merge an on-chain job with its (optional) off-chain board listing into one public view."""
    listing = listing or {}
    return {
        "job_id": job_id,
        "task": listing.get("task", ""),
        "criteria": listing.get("criteria", ""),
        "reward_usdc": listing.get("reward_usdc", on_chain.get("amount", 0) / 1_000_000),
        "client": listing.get("client") or on_chain.get("client", ""),
        "deadline": listing.get("deadline") or on_chain.get("deadline", 0),
        "posted_at": listing.get("posted_at", 0),
        "salt": listing.get("salt", ""),
        "spec_irys_id": listing.get("spec_irys_id", ""),
        "on_chain_status": on_chain.get("status", "unknown"),
        "provider": on_chain.get("provider", _ZERO),
        "committee_size": on_chain.get("committee_size", 0),
        "quorum": on_chain.get("quorum", 0),
    }


@app.get("/marketplace/jobs")
def marketplace_jobs(status: str = "open") -> dict:
    """Discover jobs by scanning the chain (the source of truth), enriched with board listings.

    Query param `status`:
      - 'open' (default): only Funded + unclaimed jobs (available for acceptance)
      - 'all': every job on-chain (within the recent scan window)
    """
    import escrow_v4 as esc
    w3 = esc.web3()
    try:
        if status == "open":
            pairs = [(jid, j) for jid, j in esc.scan_jobs(w3, statuses={"Funded"})
                     if int(j.get("provider", _ZERO), 16) == 0]
        else:
            pairs = list(esc.scan_jobs(w3))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"chain read failed: {e}")
    jobs = [_listing_view(jid, j, autonomous._listing(jid)) for jid, j in pairs]
    return {"count": len(jobs), "jobs": jobs}


@app.get("/marketplace/jobs/{job_id}")
def marketplace_job(job_id: int) -> dict:
    """One job: on-chain status merged with its board listing. Lets a provider confirm it won the race
    (provider == its address) and lets anyone inspect a job's lifecycle state."""
    import escrow_v4 as esc
    w3 = esc.web3()
    try:
        on_chain = esc.get_job(w3, job_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found: {e}")
    if on_chain.get("status") in (None, "None"):
        raise HTTPException(status_code=404, detail=f"job {job_id} does not exist")
    view = _listing_view(job_id, on_chain, autonomous._listing(job_id))
    # Committee provenance so a provider (or a human) can see whether the committee is platform-operated
    # before trusting the job — the anti free-work signal. (Cheap for a single job; omitted from the list.)
    try:
        members = esc.get_committee(w3, job_id)
        trusted = registry.trusted_evaluator_addrs()
        view["committee"] = members
        view["committee_trusted"] = sum(1 for m in members if m.lower() in trusted)
        view["committee_ok"] = view["committee_trusted"] >= max(1, on_chain.get("quorum", 1))
    except Exception:
        pass
    return view


@app.get("/marketplace/post-calldata")
def marketplace_post_calldata(client_address: str, amount_usdc: float, task: str = "",
                              criteria: str = "", committee: str = "", quorum: int = 0,
                              deadline_days: int = 7) -> dict:
    """Build the createJob/approve/fund calldata an external CLIENT signs with its own CAW wallet to open
    and fund a job. v4 names an evaluator COMMITTEE: pass `committee` as a comma-separated list of addresses
    (odd N) and an optional `quorum` (defaults to a strict majority, config.COMMITTEE_QUORUM).

    The returned `salt` binds the spec hash id-INDEPENDENTLY, so createJob + the listing are race-safe:
    sign createJob, read the REAL job id from the createJob receipt, then approve + fund(realId) (use
    GET /marketplace/jobs/{id}/fund-calldata if the real id differs from predicted_job_id), then publish
    the listing via POST /marketplace/jobs with the same `salt`.
    """
    import escrow_v4 as esc
    from web3 import Web3
    committee_list = [a.strip() for a in committee.split(",") if a.strip()]
    if not committee_list:
        raise HTTPException(status_code=400, detail="committee is required: pass a comma-separated list of "
                            "evaluator addresses (odd N), e.g. committee=0x..,0x..,0x..")
    q = quorum or config.COMMITTEE_QUORUM
    if amount_usdc <= 0 or amount_usdc > config.MAX_JOB_REWARD_USDC:
        raise HTTPException(status_code=400,
                            detail=f"amount_usdc must be in (0, {config.MAX_JOB_REWARD_USDC}] USDC "
                                   f"(per-job real-money ceiling — raise MAX_JOB_REWARD_USDC to widen)")
    w3 = esc.web3()
    try:
        job_id = esc.next_job_id(w3)  # predicted only — a hint for the fund step; the hash no longer uses it
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"chain read failed: {e}")
    salt = esc.random_salt_hex()
    spec_hash_b = esc.spec_hash(task, criteria, salt)
    amt = int(round(amount_usdc * 1_000_000))
    deadline = int(time.time()) + max(1, deadline_days) * 24 * 3600
    return {
        "predicted_job_id": job_id,
        "salt": salt,
        "spec_hash": Web3.to_hex(spec_hash_b),
        "amount_usdc": amount_usdc,
        "deadline": deadline,
        "committee": committee_list,
        "quorum": q,
        "chain_id": config.CHAIN_ID,
        "escrow": config.ESCROW_V4_ADDRESS,
        "usdc": config.USDC_ADDRESS,
        "steps": [
            {"step": "createJob", "to": config.ESCROW_V4_ADDRESS,
             "function": "createJob(address[],uint8,uint256,bytes32,uint64)",
             "calldata": esc.create_job(committee_list, q, amt, spec_hash_b, deadline)},
            {"step": "approve", "to": config.USDC_ADDRESS,
             "function": "approve(address,uint256)",
             "calldata": esc.approve(config.ESCROW_V4_ADDRESS, amt)},
            {"step": "fund", "to": config.ESCROW_V4_ADDRESS,
             "function": "fund(uint256)",
             "calldata": esc.fund(job_id)},
        ],
        "note": "createJob's spec hash is salt-bound (id-independent). Read the REAL job id from the createJob "
                "receipt; if it differs from predicted_job_id, refetch fund calldata from "
                "GET /marketplace/jobs/{id}/fund-calldata before funding. Then POST /marketplace/jobs with this salt.",
    }


class PostJobBody(BaseModel):
    job_id: int
    task: str
    criteria: str = ""
    salt: str = ""            # the salt returned by /marketplace/post-calldata (binds the spec hash)
    reward_usdc: float | None = None


@app.post("/marketplace/jobs")
def marketplace_post_job(body: PostJobBody) -> dict:
    """Publish the human-readable listing for an already-funded on-chain job so providers can discover the
    task text (only specHash lives on-chain). The job must be Funded + unclaimed (verified on-chain), and
    the posted task/criteria MUST reproduce the job's on-chain specHash — so an anonymous caller cannot
    poison the board (prompt-inject providers, misstate the reward) for a job it did not fund."""
    import escrow_v4 as esc
    from web3 import Web3
    if len(body.task) > _MAX_TASK_LEN or len(body.criteria) > _MAX_CRITERIA_LEN:
        raise HTTPException(status_code=413, detail="task/criteria exceed the length limit")
    w3 = esc.web3()
    try:
        job = esc.get_job(w3, body.job_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"job {body.job_id} not found: {e}")
    if job["status"] != "Funded":
        raise HTTPException(status_code=400, detail=f"job {body.job_id} is not Funded (status: {job['status']})")
    if int(job["provider"], 16) != 0:
        raise HTTPException(status_code=400, detail=f"job {body.job_id} already has a provider")
    # Bind the listing to what was funded: only the exact spec the client committed on-chain can be posted.
    # The hash is salt-bound (id-independent), so the real receipt id always reproduces it.
    expected = Web3.to_hex(esc.spec_hash(body.task, body.criteria, body.salt))
    if expected.lower() != str(job["spec_hash"]).lower():
        raise HTTPException(status_code=403, detail="task/criteria/salt do not match the job's on-chain "
                            "specHash (only the funded spec can be listed)")
    # The reward shown to providers is always the on-chain escrow amount — never a caller-supplied number.
    reward = job["amount"] / 1_000_000
    # Best-effort: persist the spec to Irys (durable + independently hash-verifiable), so the task text
    # isn't only in this operator's board.json. Never fail the publish on an Irys hiccup — the board carries
    # the text regardless. (A fully trustless cross-operator pointer would live on-chain — a v5/Base item.)
    spec_irys_id = ""
    try:
        import irys_store
        up = irys_store.upload(esc.spec_text(body.task, body.criteria),
                               {"app": "AgentWorks", "kind": "spec", "job-id": str(body.job_id),
                                "spec-hash": job["spec_hash"]})
        spec_irys_id = up.get("id", "")
    except Exception:
        spec_irys_id = ""
    autonomous._post_listing(body.job_id, task=body.task, criteria=body.criteria, reward_usdc=reward,
                             spec_hash=job["spec_hash"], client=job["client"], deadline=job["deadline"],
                             salt=body.salt, spec_irys_id=spec_irys_id)
    return {"posted": True, **_listing_view(body.job_id, job, autonomous._listing(body.job_id))}


class DeliverBody(BaseModel):
    deliverable: str


@app.post("/marketplace/jobs/{job_id}/deliver")
def marketplace_deliver(job_id: int, body: DeliverBody) -> dict:
    """Provider deliver helper: store the work on Irys and return the submitWork calldata the provider signs
    with its OWN CAW wallet. The platform never signs or holds provider keys - it stores + encodes only."""
    import escrow_v4 as esc
    import irys_store
    from web3 import Web3
    w3 = esc.web3()
    try:
        job = esc.get_job(w3, job_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found: {e}")
    if job["status"] != "Accepted":
        raise HTTPException(status_code=400, detail=f"job {job_id} is not Accepted (status: {job['status']})")
    if not body.deliverable.strip():
        raise HTTPException(status_code=400, detail="deliverable is empty")
    if len(body.deliverable) > _MAX_DELIVERABLE_LEN:
        raise HTTPException(status_code=413, detail="deliverable exceeds the size limit")
    dhash = Web3.keccak(text=body.deliverable)
    try:
        irys = irys_store.upload(body.deliverable,
            {"app": "AgentWorks", "job-id": str(job_id), "content-keccak": Web3.to_hex(dhash)})
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Irys upload failed: {type(e).__name__}: {e}")
    return {
        "job_id": job_id,
        "irys_id": irys.get("id"),
        "irys_url": irys.get("url"),
        "deliverable_hash": Web3.to_hex(dhash),
        "contract_address": config.ESCROW_V4_ADDRESS,
        "chain_id": config.CHAIN_ID,
        "function": "submitWork(uint256,bytes32,string)",
        "calldata": esc.submit_work(job_id, dhash, irys.get("id")),
    }


class RegisterBody(BaseModel):
    wallet_id: str
    api_key: str
    address: str
    role: str = "provider"
    name: str | None = None
    tx_cap: int = 0


@app.post("/marketplace/register")
async def marketplace_register(body: RegisterBody, authorization: str = Header(default="")) -> dict:
    """Register an external agent in the marketplace.

    The platform creates a scoped Pact for the agent using the parameterized template.
    The agent can then discover jobs via GET /marketplace/jobs and call acceptJob directly
    on-chain with their own CAW wallet.

    Required: wallet_id, api_key, address (from the agent's CAW wallet).
    The api_key is used to create the Pact and is persisted in registry.local.json (gitignored).
    Set AGENT_REGISTER_TOKEN to gate onboarding (curated pool); leave it unset for open self-service.
    """
    _require_token(authorization, _REGISTER_TOKEN)
    try:
        result = await registry.register_external(
            wallet_id=body.wallet_id,
            api_key=body.api_key,
            address=body.address,
            role=body.role,
            name=body.name,
            tx_cap=body.tx_cap,
        )
        return {**result, "custodial": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Registration failed: {type(e).__name__}: {e}")


# ── Committee verdict service (evaluation-as-a-service) ──
# The AgentWorks M-of-N committee, exposed as a framework-neutral verdict primitive. Any integrator POSTs a
# job's spec + deliverable and gets one quorum verdict back; the on-chain escrow is NOT involved. This is the
# reusable adjudication pillar — the Virtuals/ACP adapter (agents/acp-node/) is the FIRST integration to consume
# it (mapping `accept` to ACP's session.complete/reject); others integrate the same way. See docs/INTEGRATIONS.md.
#
# FROZEN integration contract:  {spec, deliverable, quorum?}  →  {accept, approve, reject, quorum, reasons[]}
class VerdictBody(BaseModel):
    spec: str
    deliverable: str
    quorum: int | None = None


def _committee_verdict(body: VerdictBody, authorization: str) -> dict:
    """Run the M-of-N committee over (spec, deliverable) and return a single quorum verdict. Gated by
    VERDICT_TOKEN (legacy ACP_VERDICT_TOKEN honored) when set. Each registered evaluator judges independently
    (own persona + model) and a quorum decides — the on-chain escrow is NOT involved."""
    _require_token(authorization, _VERDICT_TOKEN)
    if len(body.spec) > _MAX_TASK_LEN + _MAX_CRITERIA_LEN:
        raise HTTPException(status_code=413, detail="spec exceeds the size limit")
    if len(body.deliverable) > _MAX_DELIVERABLE_LEN:
        raise HTTPException(status_code=413, detail="deliverable exceeds the size limit")
    if not body.spec.strip() or not body.deliverable.strip():
        raise HTTPException(status_code=400, detail="spec and deliverable are both required")
    import reasoning
    try:
        return reasoning.committee_verdict(body.spec, body.deliverable, quorum=body.quorum)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"verdict failed: {type(e).__name__}: {e}")


@app.post("/committee/verdict")
def committee_verdict(body: VerdictBody, authorization: str = Header(default="")) -> dict:
    """Framework-neutral evaluation-as-a-service: the M-of-N committee verdict over (spec, deliverable)."""
    return _committee_verdict(body, authorization)


@app.post("/acp/verdict")
def acp_verdict(body: VerdictBody, authorization: str = Header(default="")) -> dict:
    """DEPRECATED alias of POST /committee/verdict — kept so the existing Virtuals adapter keeps working during
    the extraction. New integrations should call /committee/verdict."""
    return _committee_verdict(body, authorization)


class DirectoryBody(BaseModel):
    address: str
    wallet_id: str
    role: str = "provider"          # client | provider | evaluator
    name: str | None = None
    pact_id: str | None = None
    owner_mode: str = "unpaired"    # unpaired (agent-owned) | paired (human-owned, Cobo-app approval)
    llm_model: str = ""


@app.post("/marketplace/register-directory")
def marketplace_register_directory(body: DirectoryBody, authorization: str = Header(default="")) -> dict:
    """NON-CUSTODIAL registration (the trustless path). Records a KEYLESS discovery entry — no api_key is
    ever accepted or stored, and the platform binds NO Pact (it can't sign for an external agent). The
    operator self-binds the role's scoped Pact via MCP or the Cobo app, then lists here so others can
    discover them. The directory is DISCOVERY, not security: pact_status is self-reported; enforcement
    stays with CAW (the Pact) + the escrow. Gate with AGENT_REGISTER_TOKEN for a curated pool.
    """
    _require_token(authorization, _REGISTER_TOKEN)
    try:
        row = registry.register_directory(
            address=body.address, wallet_id=body.wallet_id, role=body.role, name=body.name,
            pact_id=body.pact_id, owner_mode=body.owner_mode, llm_model=body.llm_model, source="self",
        )
        return {"registered": True, "custodial": False, **row}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Directory registration failed: {type(e).__name__}: {e}")


@app.get("/marketplace/directory")
def marketplace_directory() -> dict:
    """The public discovery directory (keyless — no api_keys). pact_status is self-reported; enforcement
    is CAW + the escrow, so a lying entry can't sign or move funds."""
    d = registry.directory()
    return {
        "count": len(d),
        "participants": d,
        "note": "pact_status is self-reported; discovery only - enforcement is CAW (the Pact) + the escrow.",
    }


@app.get("/marketplace/jobs/{job_id}/calldata")
def marketplace_calldata(job_id: int, provider_address: str = "", salt: str = "") -> dict:
    """Get the SEALED commit-reveal accept calldata (v3) for a funded job.

    Accepting is two phases (defeats public-mempool frontrunning of the accept race):
      1. commitAccept(commitment) - commitment = keccak256(abi.encode(jobId, provider, salt)); the
         jobId stays hidden, and the hash binds to `provider` so a copied commitment is useless.
      2. revealAccept(jobId, salt) - after revealDelayBlocks, claims the job; first valid reveal wins.

    Pass `provider_address` (the wallet that will accept) so the commitment binds to it, and an
    optional 32-byte hex `salt` (else one is generated and RETURNED - the provider MUST keep it to
    reveal). External providers sign both calldatas with their OWN CAW wallet. Route the reveal
    through a private mempool for defense-in-depth (see docs/MEV.md).
    """
    import escrow_v4 as esc
    from web3 import Web3
    w3 = esc.web3()
    try:
        job = esc.get_job(w3, job_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found: {e}")
    if job["status"] != "Funded":
        raise HTTPException(status_code=400, detail=f"Job {job_id} is not available for acceptance (status: {job['status']})")
    if int(job["provider"], 16) != 0:
        raise HTTPException(status_code=400, detail=f"Job {job_id} already has a provider")
    if not provider_address:
        raise HTTPException(status_code=400, detail="provider_address is required (the commitment binds to it)")
    salt_bytes = bytes.fromhex(salt[2:] if salt.startswith("0x") else salt) if salt else esc.random_salt()
    if len(salt_bytes) != 32:
        raise HTTPException(status_code=400, detail="salt must be 32 bytes (hex)")
    commitment = esc.commitment(job_id, provider_address, salt_bytes)
    return {
        "job_id": job_id,
        "contract_address": config.ESCROW_V4_ADDRESS,
        "chain_id": config.CHAIN_ID,
        "job_status": job["status"],
        "reward_usdc": job["amount"] / 1_000_000,
        "provider_address": provider_address,
        "salt": Web3.to_hex(salt_bytes),
        "commitment": Web3.to_hex(commitment),
        "reveal_delay_blocks": config.REVEAL_DELAY_BLOCKS,
        "steps": [
            {"step": "commitAccept", "to": config.ESCROW_V4_ADDRESS,
             "function": "commitAccept(bytes32)",
             "calldata": esc.commit_accept(commitment),
             "note": "phase 1: publish the sealed bid (opaque; reveals no jobId)"},
            {"step": "revealAccept", "to": config.ESCROW_V4_ADDRESS,
             "function": "revealAccept(uint256,bytes32)",
             "calldata": esc.reveal_accept(job_id, salt_bytes),
             "note": "phase 2: after %d block(s), claim the job; keep the salt secret until now" % config.REVEAL_DELAY_BLOCKS},
        ],
        "note": "KEEP the salt - revealAccept needs it. The commitment binds to provider_address; a "
                "copied commitment cannot be revealed by anyone else.",
    }


@app.get("/marketplace/jobs/{job_id}/committee")
def marketplace_committee(job_id: int) -> dict:
    """The committee + voting + dispute state for a job (v4). Lets anyone inspect who evaluates a job,
    the running vote tally / tentative outcome, and whether the outcome has been disputed."""
    import escrow_v4 as esc
    w3 = esc.web3()
    try:
        committee = esc.get_committee(w3, job_id)
        vote = esc.get_vote(w3, job_id)
        dispute = esc.get_dispute(w3, job_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"job {job_id} committee read failed: {e}")
    return {"job_id": job_id, "committee": committee, "vote": vote, "dispute": dispute}


@app.get("/marketplace/jobs/{job_id}/vote-calldata")
def marketplace_vote_calldata(job_id: int, approve: bool = True) -> dict:
    """Build the castVote calldata an external COMMITTEE member signs with its own CAW wallet to vote on a
    submitted deliverable: approve=true pays the provider, approve=false refunds the client. Once quorum is
    reached the job is tentatively Resolved; finalize after the dispute window."""
    import escrow_v4 as esc
    return {
        "job_id": job_id,
        "contract_address": config.ESCROW_V4_ADDRESS,
        "chain_id": config.CHAIN_ID,
        "approve": approve,
        "function": "castVote(uint256,bool)",
        "calldata": esc.cast_vote(job_id, approve),
        "note": "only an address on the job's committee can vote, once; read /committee for the tally.",
    }


@app.get("/marketplace/jobs/{job_id}/finalize-calldata")
def marketplace_finalize_calldata(job_id: int) -> dict:
    """Build the finalize calldata anyone can sign to settle a Resolved job (payout or refund per the
    committee vote) once the dispute window has elapsed with no open dispute."""
    import escrow_v4 as esc
    return {
        "job_id": job_id,
        "contract_address": config.ESCROW_V4_ADDRESS,
        "chain_id": config.CHAIN_ID,
        "function": "finalize(uint256)",
        "calldata": esc.finalize(job_id),
        "note": "callable by anyone after the dispute window; settles per the committee vote.",
    }


@app.get("/marketplace/jobs/{job_id}/fund-calldata")
def marketplace_fund_calldata(job_id: int) -> dict:
    """Build the fund calldata the CLIENT signs after createJob to move USDC into escrow. Handy on the
    manual/advanced post path when the real createJob receipt id differs from the predicted one — refetch
    fund calldata for the confirmed id (createJob's spec hash is salt-bound, so it's already correct)."""
    import escrow_v4 as esc
    return {
        "job_id": job_id,
        "contract_address": config.ESCROW_V4_ADDRESS,
        "chain_id": config.CHAIN_ID,
        "function": "fund(uint256)",
        "calldata": esc.fund(job_id),
        "note": "sign with the client wallet AFTER approve; requires the job be Open and you its client.",
    }


@app.get("/marketplace/jobs/{job_id}/refund-calldata")
def marketplace_refund_calldata(job_id: int) -> dict:
    """Build the claimRefund calldata the CLIENT signs to reclaim escrow on a job that stalled before
    settlement (Funded but never accepted, or Accepted but never resolved) once its deadline has passed.
    The deadline anti-freeze exit. NOTE: the contract rejects claimRefund for a Submitted job — a stalled
    Submitted job is instead recovered by the permissionless forceResolve (→ tentative refund)."""
    import escrow_v4 as esc
    return {
        "job_id": job_id,
        "contract_address": config.ESCROW_V4_ADDRESS,
        "chain_id": config.CHAIN_ID,
        "function": "claimRefund(uint256)",
        "calldata": esc.claim_refund(job_id),
        "note": "callable by the client after the deadline for a Funded/Accepted job (not Submitted); refunds the escrow.",
    }


@app.get("/marketplace/participants")
def marketplace_participants() -> dict:
    """List all registered marketplace participants (public info only - no api_keys)."""
    pool = registry.load_pool()
    return {
        "count": len(pool),
        "providers": len(registry.providers(pool)),
        "participants": [p.public() for p in pool],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
