"""FastAPI control surface for the autonomous agent service (Phase 6.5.4).

Cloud-deployable HTTP front for the autonomous open-marketplace agents. The dashboard (Vercel)
calls this instead of spawning local processes. The service talks to the CAW cloud API over HTTPS;
on-chain SIGNING happens via the TSS node connected to the CAW relay (run alongside, or on a host
you control - see docs/DEPLOY_AGENTS.md). This process holds NO key material.

Endpoints:
  GET  /health           liveness + config summary (escrow, providers, whether a run is active)
  GET  /board            current open-job listings (off-chain marketplace listing)
  GET  /runs             all run artifacts (settled + in-progress), newest first
  GET  /runs/{job_id}    one run artifact
  POST /trigger          launch an autonomous run; guarded by AGENT_TRIGGER_TOKEN if that env is set

A single run executes at a time (the autonomous loops settle `max_jobs` then return).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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

_TOKEN = os.environ.get("AGENT_TRIGGER_TOKEN", "")  # if set, /trigger requires it
_REGISTER_TOKEN = os.environ.get("AGENT_REGISTER_TOKEN", "")  # if set, /marketplace/register requires it

if not _TOKEN:
    log.warning("AGENT_TRIGGER_TOKEN is not set — POST /trigger is OPEN to anyone and drives the "
                "TSS-signing wallets. Set it before exposing this service publicly.")

# Bounds on caller-supplied text so an anonymous request can't bloat the board / platform Irys account.
_MAX_TASK_LEN = 2000
_MAX_CRITERIA_LEN = 2000
_MAX_DELIVERABLE_LEN = 100_000  # 100 KB cap on platform-funded Irys uploads


def _require_token(authorization: str, token: str) -> None:
    """Bearer-token gate. Open when `token` is empty; else require `Authorization: Bearer <token>`."""
    if token and authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="missing/invalid bearer token")


def _spec_hash(task: str, criteria: str, job_id: int) -> "bytes":
    """Canonical job spec-hash = keccak256("<spec>#<jobId>"). Must stay identical to the expression a
    client funds with (marketplace_post_calldata) so marketplace_post_job can re-derive it and bind a
    listing to the job that was actually funded on-chain."""
    from web3 import Web3
    spec = f"{task}\n\nAcceptance criteria: {criteria}".strip() if criteria else task
    return Web3.keccak(text=f"{spec}#{job_id}")


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


# single-run guard
_state: dict = {"active": False, "run_id": None, "started_at": None, "mode": None, "last_error": None}
_task: asyncio.Task | None = None

# settlement watcher (standing liveness that finalizes/settles EXTERNAL jobs the platform doesn't drive)
_WATCHER_ENABLED = os.environ.get("SETTLEMENT_WATCHER", "1") != "0"
_watcher_task: asyncio.Task | None = None


@app.on_event("startup")
async def _start_watcher() -> None:
    """Launch the settlement watcher so jobs posted+claimed+voted entirely by EXTERNAL agents still settle
    (finalize is permissionless + outcome-neutral). No-ops without a first-party signer or when disabled."""
    global _watcher_task
    if not _WATCHER_ENABLED:
        log.info("[watcher] disabled (SETTLEMENT_WATCHER=0)")
        return
    if not autonomous.has_first_party_signer():
        log.info("[watcher] no first-party signer; external jobs settle via /marketplace/jobs/{id}/finalize-calldata")
        return
    _watcher_task = asyncio.create_task(autonomous.settlement_watcher())


@app.on_event("shutdown")
async def _stop_watcher() -> None:
    if _watcher_task and not _watcher_task.done():
        _watcher_task.cancel()


def _watcher_active() -> bool:
    return bool(_watcher_task and not _watcher_task.done())


class TriggerBody(BaseModel):
    task: str | None = None
    criteria: str = ""
    mode: str = "good"          # 'good' | 'bad'
    reward_usdc: float = 5.0
    max_jobs: int = 1


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
        "run": {k: _state[k] for k in ("active", "run_id", "mode", "started_at")},
        "trigger_protected": bool(_TOKEN),
        "register_protected": bool(_REGISTER_TOKEN),
        "watcher": {"enabled": _WATCHER_ENABLED, "active": _watcher_active()},
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


async def _run(body: TriggerBody) -> None:
    try:
        tasks = ([{"task": body.task, "criteria": body.criteria}] if body.task
                 else autonomous._default_tasks())
        out = await autonomous.run_market(tasks, mode=body.mode, reward_usdc=body.reward_usdc,
                                          max_jobs=body.max_jobs)
        _state["run_id"] = out.get("run_id")
        log.info("[server] run complete: %s", out.get("run_id"))
    except Exception as e:  # surface, don't hide
        _state["last_error"] = f"{type(e).__name__}: {e}"
        log.exception("[server] run failed")
    finally:
        _state["active"] = False


@app.post("/trigger")
async def trigger(body: TriggerBody, authorization: str = Header(default="")) -> dict:
    _require_token(authorization, _TOKEN)
    if _state["active"]:
        raise HTTPException(status_code=409, detail="a run is already active")
    if body.mode not in ("good", "bad"):
        raise HTTPException(status_code=400, detail="mode must be 'good' or 'bad'")
    global _task
    _state.update({"active": True, "run_id": None, "started_at": int(time.time()),
                   "mode": body.mode, "last_error": None})
    _task = asyncio.create_task(_run(body))
    return {"accepted": True, "mode": body.mode, "reward_usdc": body.reward_usdc,
            "max_jobs": body.max_jobs, "poll": "/runs"}


# ── Open Marketplace API ────────────────────────────────────────────────────
# These endpoints turn the internal orchestrator into a public marketplace.
# External agents bring their own CAW wallet, register, discover jobs (the chain
# is the source of truth), fund/post, accept, and deliver. The platform holds NO
# external keys - it returns calldata the agent signs with its own CAW wallet.

_ZERO = "0x0000000000000000000000000000000000000000"
_SCAN_DEPTH = 200  # how many recent job ids to scan during discovery


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
        n = esc.next_job_id(w3)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"chain read failed: {e}")
    lo = max(1, n - _SCAN_DEPTH)
    jobs = []
    for job_id in range(n - 1, lo - 1, -1):
        try:
            on_chain = esc.get_job(w3, job_id)
        except Exception:
            continue
        if on_chain.get("status") in (None, "None"):
            continue
        if status == "open" and not (
            on_chain.get("status") == "Funded" and int(on_chain.get("provider", _ZERO), 16) == 0
        ):
            continue
        jobs.append(_listing_view(job_id, on_chain, autonomous._listing(job_id)))
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
    return _listing_view(job_id, on_chain, autonomous._listing(job_id))


@app.get("/marketplace/post-calldata")
def marketplace_post_calldata(client_address: str, amount_usdc: float, task: str = "",
                              criteria: str = "", committee: str = "", quorum: int = 0,
                              deadline_days: int = 7) -> dict:
    """Build the createJob/approve/fund calldata an external CLIENT signs with its own CAW wallet to open
    and fund a job. v4 names an evaluator COMMITTEE: pass `committee` as a comma-separated list of addresses
    (odd N) and an optional `quorum` (defaults to a strict majority, config.COMMITTEE_QUORUM). After funding,
    the client publishes the human-readable listing via POST /marketplace/jobs.

    NOTE: job_id is predicted from the current nextJobId - if two clients fund in the same block the
    prediction can race; re-read the real id from the createJob receipt before posting the listing.
    """
    import escrow_v4 as esc
    from web3 import Web3
    committee_list = [a.strip() for a in committee.split(",") if a.strip()]
    if not committee_list:
        raise HTTPException(status_code=400, detail="committee is required: pass a comma-separated list of "
                            "evaluator addresses (odd N), e.g. committee=0x..,0x..,0x..")
    q = quorum or config.COMMITTEE_QUORUM
    w3 = esc.web3()
    try:
        job_id = esc.next_job_id(w3)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"chain read failed: {e}")
    spec_hash_b = _spec_hash(task, criteria, job_id)
    amt = int(round(amount_usdc * 1_000_000))
    deadline = int(time.time()) + max(1, deadline_days) * 24 * 3600
    return {
        "predicted_job_id": job_id,
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
        "note": "job_id is predicted from nextJobId; verify the real id from the createJob receipt before POST /marketplace/jobs.",
    }


class PostJobBody(BaseModel):
    job_id: int
    task: str
    criteria: str = ""
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
    expected = Web3.to_hex(_spec_hash(body.task, body.criteria, body.job_id))
    if expected.lower() != str(job["spec_hash"]).lower():
        raise HTTPException(status_code=403, detail="task/criteria do not match the job's on-chain "
                            "specHash (only the funded spec can be listed)")
    # The reward shown to providers is always the on-chain escrow amount — never a caller-supplied number.
    reward = job["amount"] / 1_000_000
    autonomous._post_listing(body.job_id, task=body.task, criteria=body.criteria, reward_usdc=reward,
                             spec_hash=job["spec_hash"], client=job["client"], deadline=job["deadline"])
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


@app.get("/marketplace/jobs/{job_id}/refund-calldata")
def marketplace_refund_calldata(job_id: int) -> dict:
    """Build the claimRefund calldata the CLIENT signs to reclaim escrow on a job that stalled before
    settlement (Funded but never accepted, or Accepted/Submitted but never resolved) once its deadline has
    passed. The deadline anti-freeze exit — finalize/resolveTimeout only cover Resolved/Disputed."""
    import escrow_v4 as esc
    return {
        "job_id": job_id,
        "contract_address": config.ESCROW_V4_ADDRESS,
        "chain_id": config.CHAIN_ID,
        "function": "claimRefund(uint256)",
        "calldata": esc.claim_refund(job_id),
        "note": "callable by the client after the deadline for a Funded/Accepted/Submitted job; refunds the escrow.",
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
