"""AgentWorks MCP server - the open agent socket.

Run locally by an operator with their OWN Cobo Agentic Wallet. Exposes the AgentWorks marketplace as MCP tools
so ANY MCP-capable agent (Claude Desktop / Claude Code / etc.) can be a client or a provider. The genuine
"agent" is the connecting LLM; this server just ships the socket.

Trustless by construction: calldata is built locally (escrow_v4), signed through the operator's OWN CAW wallet
(Pact-scoped), and only the shared off-chain board is read from the deployed service. The operator's api_key
never leaves this process, and the Pact is the hard boundary regardless of what the connecting LLM decides
(a provider Pact excludes USDC, so a provider can accept + deliver but can never move escrowed funds).

Accepting a job is the SEALED commit-reveal race (carried from v3): a provider commits an opaque hash (the
targeted jobId stays hidden from the public mempool), waits the reveal delay, then reveals to claim. accept_job()
runs both phases; commit_accept()/reveal_accept() expose them individually for demos. See docs/MEV.md.

Settlement is v4 committee consensus: the client names an evaluator COMMITTEE at post time. Each member
cast_vote()s on the deliverable; once quorum is reached the job is tentatively Resolved, anyone can finalize()
after the dispute window, and the losing side may dispute() (escalating, with a staked UMA bond, to a decoupled
arbiter — never an operator key). See docs/ARBITRATION.md.

Config (env - the operator's own wallet):
  MCP_WALLET_ID, MCP_API_KEY, MCP_ADDRESS, MCP_ROLE=client|provider|evaluator
  MCP_PUBLISH_DIRECTORY=1 (opt-in: after onboard, publish a KEYLESS directory entry for discovery; no api_key sent)
  AGENT_API (marketplace board base url; defaults to the droplet agent service http://139.59.135.74:8000)
  + reused from config.py / .env: RPC_URL, ESCROW_V4_CONTRACT_ADDRESS, USDC_TOKEN_ADDRESS, CAW_CHAIN_ID, IRYS_*

Run:  python agents/mcp_server.py            # stdio (for Claude Desktop / Code)
Signing tools need the operator's TSS node connected to the CAW relay; read tools do not.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from uuid import uuid4
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling imports regardless of cwd

import requests
from web3 import Web3
from mcp.server.fastmcp import FastMCP

import config
import escrow_v4 as esc
import irys_store
import pacts
from caw import CawWallet

MCP_WALLET_ID = os.environ.get("MCP_WALLET_ID", "")
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")
MCP_ADDRESS = os.environ.get("MCP_ADDRESS", "")
MCP_ROLE = (os.environ.get("MCP_ROLE", "provider") or "provider").lower()
AGENT_API = (os.environ.get("AGENT_API") or os.environ.get("NEXT_PUBLIC_AGENT_API")
             or "http://139.59.135.74:8000").rstrip("/")
EXPLORER = (os.environ.get("NEXT_PUBLIC_EXPLORER_BASE") or "https://sepolia.etherscan.io").rstrip("/")

mcp = FastMCP("agentworks")

# Cached operator wallet bound to an active Pact (lazy; created on first signing tool or onboard()).
_scoped: CawWallet | None = None
_pact: dict | None = None
# Sealed accept bids in flight: job_id -> salt, so commit_accept() and a later reveal_accept() (or the
# accept_job() orchestrator) share the secret. Held only in this process.
_salts: dict[int, bytes] = {}


# ── helpers ──────────────────────────────────────────────────────────────────

def _tx(h: str) -> dict:
    return {"hash": h, "url": f"{EXPLORER}/tx/{h}" if h else ""}


async def _get(path: str) -> Any:
    def _do():
        r = requests.get(f"{AGENT_API}{path}", timeout=30)
        r.raise_for_status()
        return r.json()
    return await asyncio.to_thread(_do)


async def _post(path: str, body: dict) -> Any:
    def _do():
        r = requests.post(f"{AGENT_API}{path}", json=body, timeout=30)
        r.raise_for_status()
        return r.json()
    return await asyncio.to_thread(_do)


async def _revoke_active(w: CawWallet) -> None:
    try:
        page = await w.list_pacts(status="active")
    except Exception:
        return
    items = page if isinstance(page, list) else (page.get("items", []) if isinstance(page, dict) else [])
    for p in items:
        if isinstance(p, dict) and p.get("id"):
            try:
                await w.revoke_pact(p["id"])
            except Exception:
                pass


async def _ensure_onboarded() -> CawWallet:
    """Submit the role's scoped Pact on the operator's OWN wallet and cache the pact-scoped client.
    Idempotent for the process lifetime (revokes stale active pacts first for a clean slate)."""
    global _scoped, _pact
    if _scoped is not None:
        return _scoped
    if not (MCP_WALLET_ID and MCP_API_KEY and MCP_ADDRESS):
        raise RuntimeError("set MCP_WALLET_ID / MCP_API_KEY / MCP_ADDRESS to your own CAW wallet")
    if MCP_ROLE not in ("client", "provider", "evaluator"):
        raise RuntimeError("MCP_ROLE must be 'client', 'provider', or 'evaluator'")
    root = CawWallet(api_url=config.CAW_API_URL, api_key=MCP_API_KEY, wallet_uuid=MCP_WALLET_ID, name=f"mcp-{MCP_ROLE}")
    await _revoke_active(root)
    if MCP_ROLE == "provider":
        spec = pacts.provider_pact(escrow=config.ESCROW_V4_ADDRESS)
    elif MCP_ROLE == "evaluator":
        spec = pacts.evaluator_pact(escrow=config.ESCROW_V4_ADDRESS)
    else:
        spec = pacts.client_escrow_pact(escrow=config.ESCROW_V4_ADDRESS, usdc=config.USDC_ADDRESS)
    sub = await root.submit_pact(intent=f"AgentWorks MCP {MCP_ROLE}", spec=spec,
                                 name=f"mcp-{MCP_ROLE}-{MCP_WALLET_ID[:8]}")
    _pact = await root.wait_pact_active(sub.get("pact_id"))
    _scoped = root.scoped(_pact)
    return _scoped


async def _sign(target: str, calldata: str, label: str, *, private_tx: bool = False) -> str:
    """contract_call through the operator's Pact-scoped wallet → wait for finality → return the tx hash."""
    w = await _ensure_onboarded()
    rid = f"mcp-{uuid4().hex[:10]}"
    resp = await w.contract_call(src_addr=MCP_ADDRESS, contract_addr=target, calldata=calldata,
                                 chain_id=config.CHAIN_ID, request_id=rid, description=label,
                                 private_tx=private_tx)
    rec = await w.wait_tx_final(rid, timeout=420.0)
    return (rec or {}).get("transaction_hash") or (resp or {}).get("transaction_hash") or ""


# ── discovery tools (no signing) ─────────────────────────────────────────────

@mcp.tool()
async def list_open_jobs() -> dict:
    """List funded, unclaimed jobs available to accept (chain-true), with their human-readable task text."""
    return await _get("/marketplace/jobs?status=open")


@mcp.tool()
async def list_all_jobs() -> dict:
    """List all recent marketplace jobs with on-chain status (open, accepted, submitted, settled)."""
    return await _get("/marketplace/jobs?status=all")


@mcp.tool()
async def get_job(job_id: int) -> dict:
    """Get one job's on-chain status + listing. Use after accept_job to confirm you won the race
    (provider == your address)."""
    return await _get(f"/marketplace/jobs/{job_id}")


@mcp.tool()
async def get_deliverable(job_id: int) -> dict:
    """Fetch the submitted deliverable from Irys for a job, so an evaluator agent can judge it against the spec."""
    w3 = esc.web3()
    job = await asyncio.to_thread(esc.get_job, w3, job_id)
    irys_id = job.get("irys_id") or ""
    if not irys_id:
        return {"job_id": job_id, "status": job.get("status"), "deliverable": None,
                "note": "no deliverable submitted yet"}
    content = (await asyncio.to_thread(irys_store.fetch, irys_id)).decode("utf-8", "replace")
    return {"job_id": job_id, "irys_id": irys_id, "status": job.get("status"),
            "content_hash_onchain": job.get("deliverable_hash"), "deliverable": content}


@mcp.tool()
async def marketplace_participants() -> dict:
    """List the seeded marketplace participants (public info - no keys)."""
    return await _get("/marketplace/participants")


@mcp.tool()
async def my_wallet() -> dict:
    """Show this operator's CAW wallet identity, role, native + USDC balances, and active-Pact status."""
    if not (MCP_WALLET_ID and MCP_API_KEY and MCP_ADDRESS):
        return {"configured": False, "note": "set MCP_WALLET_ID / MCP_API_KEY / MCP_ADDRESS to your CAW wallet"}
    out: dict = {"configured": True, "role": MCP_ROLE, "address": MCP_ADDRESS, "wallet_id": MCP_WALLET_ID,
                 "onboarded": _scoped is not None, "pact_id": (_pact or {}).get("id") if _pact else None,
                 "escrow_v4": config.ESCROW_V4_ADDRESS, "chain": "Ethereum Sepolia"}
    try:
        w3 = esc.web3()
        out["eth"] = w3.eth.get_balance(Web3.to_checksum_address(MCP_ADDRESS)) / 1e18
        out["usdc"] = (await asyncio.to_thread(esc.usdc_balance, w3, MCP_ADDRESS)) / 1_000_000
    except Exception as e:
        out["balance_note"] = f"balance read failed: {type(e).__name__}"
    return out


@mcp.tool()
async def workflow_guide() -> dict:
    """How to participate. Returns the client / provider / evaluator step sequences (which tools, in order)."""
    return {
        "your_role": MCP_ROLE,
        "provider": ["onboard()", "list_open_jobs()", "(decide which job is worth the reward)",
                     "accept_job(job_id)  # sealed commit-reveal race (both phases); check 'won'",
                     "(do the work)", "deliver_work(job_id, deliverable)",
                     "(if you lose the committee vote) dispute(job_id)  # stakes a UMA bond; escalates to the arbiter"],
        "client": ["onboard()", "post_job(task, criteria, reward_usdc, committee=[...], quorum=2)",
                   "(wait; poll get_job(job_id) until status == 'Submitted')",
                   "(committee members cast_vote; once quorum is reached status -> 'Resolved')",
                   "finalize(job_id)  # after the dispute window, anyone may finalize",
                   "(if you disagree with the outcome) dispute(job_id)"],
        "evaluator": ["onboard()", "list_all_jobs()  # find Submitted jobs you are on the committee for",
                      "get_deliverable(job_id)  # judge it against the spec",
                      "cast_vote(job_id, approve=True|False)  # quorum of votes resolves the job",
                      "finalize(job_id)  # optional: anyone may finalize after the dispute window"],
        "note": "Authority is your Pact, not this server. Provider + evaluator Pacts exclude USDC, so you can "
                "accept/deliver/vote but can never move escrowed funds; the escrow contract settles after quorum "
                "+ the dispute window. A contested outcome escalates (with a staked bond) to a decoupled arbiter.",
    }


# ── onboarding (self-Pact, trustless) ────────────────────────────────────────

@mcp.tool()
async def onboard() -> dict:
    """Bind this wallet's scoped Pact (the authority boundary) on YOUR OWN CAW wallet. Trustless: your api_key
    never leaves this process. Provider Pact = escrow-only (no USDC); client Pact = escrow + USDC, tx-capped."""
    await _ensure_onboarded()
    _policies = {
        "provider": "provider: escrow-only, USDC excluded",
        "evaluator": "evaluator: escrow-only (castVote), USDC excluded",
        "client": "client: escrow + USDC allowlist, tx-capped",
    }
    # Opt-in: after self-binding the Pact locally, publish a KEYLESS directory entry so other agents can
    # discover this one. Only non-secret identity is sent (never the api_key). Best-effort — never blocks onboard.
    published = False
    if os.environ.get("MCP_PUBLISH_DIRECTORY", "").lower() in ("1", "true", "yes"):
        try:
            await _post("/marketplace/register-directory", {
                "role": MCP_ROLE, "address": MCP_ADDRESS, "wallet_id": MCP_WALLET_ID,
                "pact_id": (_pact or {}).get("id"), "owner_mode": "unpaired",
            })
            published = True
        except Exception:
            published = False
    return {"onboarded": True, "role": MCP_ROLE, "address": MCP_ADDRESS,
            "pact_id": (_pact or {}).get("id"), "pact_status": (_pact or {}).get("status"),
            "policy": _policies.get(MCP_ROLE, ""), "directory_published": published}


# ── client tools (sign via the operator's wallet) ────────────────────────────

@mcp.tool()
async def post_job(task: str, criteria: str = "", reward_usdc: float = 5.0, deadline_days: int = 7,
                   committee: list[str] = None, quorum: int = None) -> dict:
    """[client] Open + fund a job through YOUR OWN wallet (createJob → approve → fund), then publish the listing
    so providers can discover the task. You MUST name an evaluator COMMITTEE (a list of addresses, odd N) that
    will vote on the deliverable; quorum defaults to a strict majority (config.COMMITTEE_QUORUM). Returns the
    job_id + tx hashes."""
    if MCP_ROLE != "client":
        return {"error": "MCP_ROLE must be 'client' to post jobs"}
    committee = committee or []
    if not committee:
        return {"error": "committee is required: pass a list of evaluator addresses (odd N) that will vote on "
                         "the deliverable, e.g. committee=['0x..','0x..','0x..']"}
    q = quorum or config.COMMITTEE_QUORUM
    w3 = esc.web3()
    # Salt-bound spec hash is id-INDEPENDENT, so we can read the REAL job id from the createJob receipt
    # (not the predicted nextJobId) and fund + publish against it — no same-block id race.
    salt = esc.random_salt_hex()
    spec_hash = esc.spec_hash(task, criteria, salt)
    amt = int(round(reward_usdc * 1_000_000))
    deadline = int(time.time()) + max(1, deadline_days) * 24 * 3600
    tx_create = await _sign(config.ESCROW_V4_ADDRESS,
                            esc.create_job(committee, q, amt, spec_hash, deadline), "createJob")
    job_id = await asyncio.to_thread(esc.job_id_from_receipt, w3, tx_create)
    tx_approve = await _sign(config.USDC_ADDRESS, esc.approve(config.ESCROW_V4_ADDRESS, amt), "approve")
    tx_fund = await _sign(config.ESCROW_V4_ADDRESS, esc.fund(job_id), "fund")
    listing: Any = None
    try:
        def _pub():
            r = requests.post(f"{AGENT_API}/marketplace/jobs",
                              json={"job_id": job_id, "task": task, "criteria": criteria,
                                    "salt": salt, "reward_usdc": reward_usdc},
                              timeout=30)
            return r.json() if r.status_code < 400 else {"warn": f"HTTP {r.status_code}: {r.text[:200]}"}
        listing = await asyncio.to_thread(_pub)
    except Exception as e:
        listing = {"warn": f"listing publish failed: {type(e).__name__}: {e}"}
    return {"job_id": job_id, "committee": committee, "quorum": q, "reward_usdc": reward_usdc,
            "txs": {"createJob": _tx(tx_create), "approve": _tx(tx_approve), "fund": _tx(tx_fund)},
            "listing_published": listing, "next": "providers can now accept_job(%d); poll get_job(%d)" % (job_id, job_id)}


@mcp.tool()
async def cast_vote(job_id: int, approve: bool) -> dict:
    """[evaluator] Cast your committee vote on a submitted deliverable: approve → pay the provider,
    reject → refund the client. Only an address on the job's committee can vote (once). Once quorum is
    reached the job becomes tentatively Resolved (then finalize() after the dispute window). Read
    get_deliverable(job_id) first. Reports the running tally."""
    w3 = esc.web3()
    job = await asyncio.to_thread(esc.get_job, w3, job_id)
    if job["status"] != "Submitted":
        return {"error": f"job {job_id} is not Submitted (status: {job['status']})"}
    tx = await _sign(config.ESCROW_V4_ADDRESS, esc.cast_vote(job_id, approve), "castVote")
    vote = await asyncio.to_thread(esc.get_vote, w3, job_id)
    final = await asyncio.to_thread(esc.get_job, w3, job_id)
    return {"job_id": job_id, "voted": "approve" if approve else "reject", "tx": _tx(tx),
            "tally": {"approve": vote["approve"], "reject": vote["reject"]},
            "quorum": final["quorum"], "committee_size": final["committee_size"],
            "status": final["status"],
            "next": "finalize(%d) after the dispute window once status == 'Resolved'" % job_id}


@mcp.tool()
async def finalize(job_id: int) -> dict:
    """[any] After the committee resolves a job (quorum reached) AND the dispute window has elapsed with no
    open dispute, finalize settlement on-chain: payout to the provider or refund to the client per the vote.
    Anyone may call this. Reports the final status."""
    w3 = esc.web3()
    tx = await _sign(config.ESCROW_V4_ADDRESS, esc.finalize(job_id), "finalize")
    final = await asyncio.to_thread(esc.get_job, w3, job_id)
    return {"job_id": job_id, "tx": _tx(tx), "final_status": final["status"]}


@mcp.tool()
async def dispute(job_id: int) -> dict:
    """[losing side: client or provider] Escalate a tentatively Resolved outcome you disagree with to the
    decoupled, decentralized arbiter (UMA Optimistic Oracle V3) by staking a bond. Must be called within the
    dispute window. NOTE: you must FIRST approve the UMA arbiter (config.UMA_ARBITER_ADDRESS) to pull the bond
    currency (config.UMA_BOND_CURRENCY) for the bond amount — sign that approve() with your own wallet before
    calling this. Reports the resulting status."""
    w3 = esc.web3()
    tx = await _sign(config.ESCROW_V4_ADDRESS, esc.dispute(job_id), "dispute")
    final = await asyncio.to_thread(esc.get_job, w3, job_id)
    disp = await asyncio.to_thread(esc.get_dispute, w3, job_id)
    return {"job_id": job_id, "tx": _tx(tx), "status": final["status"], "dispute": disp,
            "arbiter": config.UMA_ARBITER_ADDRESS,
            "note": "the disputer must have approved the UMA arbiter for the bond currency before disputing"}


# ── provider tools (sign via the operator's wallet) ──────────────────────────

async def _wait_reveal_ready(w3: Web3, commit_tx: str) -> None:
    """Block until the committed tx is old enough to reveal (commitBlock + revealDelayBlocks)."""
    try:
        rcpt = await asyncio.to_thread(w3.eth.get_transaction_receipt, commit_tx)
        commit_block = int(rcpt["blockNumber"])
    except Exception:
        commit_block = await asyncio.to_thread(lambda: w3.eth.block_number)
    ready = commit_block + config.REVEAL_DELAY_BLOCKS
    for _ in range(120):
        head = await asyncio.to_thread(lambda: w3.eth.block_number)
        if head >= ready:
            return
        await asyncio.sleep(4.0)


def _committee_review(w3, job_id: int, quorum: int) -> dict:
    """Committee provenance check for a would-be provider: how many of the job's committee are known
    PLATFORM-OPERATED evaluators. `ok` means trusted members alone can reach quorum, so a client-controlled
    (sock-puppet) committee can't reject your delivered work to refund itself (the free-work attack)."""
    import registry
    try:
        members = esc.get_committee(w3, job_id)
    except Exception:
        return {"size": 0, "trusted": 0, "trusted_set_size": 0, "ok": False}
    trusted = registry.trusted_evaluator_addrs()
    known = sum(1 for m in members if m.lower() in trusted)
    return {"size": len(members), "trusted": known, "trusted_set_size": len(trusted),
            "ok": known >= max(1, quorum)}


def _committee_gate(review: dict, quorum: int) -> "str | dict | None":
    """Return an error dict to ABORT (operator opted in via PROVIDER_REQUIRE_KNOWN_COMMITTEE and has a
    trusted set), a warning string to PROCEED-with-caution, or None if the committee is trusted."""
    if review["ok"]:
        return None
    warn = (f"committee has only {review['trusted']}/{quorum} platform-operated evaluators "
            f"(of {review['size']}); a client-controlled committee could reject your work and refund itself.")
    if config.PROVIDER_REQUIRE_KNOWN_COMMITTEE and review["trusted_set_size"] > 0:
        return {"error": "committee vetting failed — " + warn, "committee_review": review}
    return warn


@mcp.tool()
async def accept_job(job_id: int) -> dict:
    """[provider] Claim a funded job via the SEALED commit-reveal race (runs BOTH phases): commit an
    opaque hash (the jobId stays hidden from the public mempool), wait the short reveal delay, then
    reveal to claim. The first valid reveal wins; a loser's reveal reverts. Re-reads the job to report
    whether you won. Vets the committee first (anti free-work). (For step-by-step control, use
    commit_accept() then reveal_accept().)"""
    w3 = esc.web3()
    job = await asyncio.to_thread(esc.get_job, w3, job_id)
    if job["status"] != "Funded":
        return {"error": f"job {job_id} is not available (status: {job['status']})"}
    review = await asyncio.to_thread(_committee_review, w3, job_id, job.get("quorum", 1))
    gate = _committee_gate(review, job.get("quorum", 1))
    if isinstance(gate, dict):
        return gate  # hard abort (operator opted into committee vetting)
    committee_warning = gate  # None or a caution string surfaced in the result
    salt = esc.random_salt()
    _salts[job_id] = salt
    try:
        commit_tx = await _sign(config.ESCROW_V4_ADDRESS,
                                esc.commit_accept(esc.commitment(job_id, MCP_ADDRESS, salt)), "commitAccept")
    except Exception as e:
        return {"won": False, "error": f"commitAccept failed: {type(e).__name__}: {e}"}
    await _wait_reveal_ready(w3, commit_tx)
    try:
        reveal_tx = await _sign(config.ESCROW_V4_ADDRESS, esc.reveal_accept(job_id, salt), "revealAccept",
                                private_tx=config.MEV_PROTECT)
    except Exception as e:
        return {"won": False, "commit_tx": _tx(commit_tx),
                "error": f"revealAccept reverted - likely lost the sealed race: {type(e).__name__}: {e}"}
    after = await asyncio.to_thread(esc.get_job, w3, job_id)
    won = int(after["provider"], 16) == int(MCP_ADDRESS, 16)
    _salts.pop(job_id, None)
    return {"job_id": job_id, "won": won, "txs": {"commitAccept": _tx(commit_tx), "revealAccept": _tx(reveal_tx)},
            "provider_now": after["provider"], "committee_warning": committee_warning,
            "next": "deliver_work(%d, <your work>)" % job_id if won else "another provider won; pick another job"}


@mcp.tool()
async def commit_accept(job_id: int) -> dict:
    """[provider] Step 1 of the sealed race: publish an opaque commitment (keccak256(jobId, you, salt)).
    Reveals NOTHING about which job you target. The salt is held in this process for reveal_accept().
    After the reveal delay (~1 block on Sepolia), call reveal_accept(job_id)."""
    w3 = esc.web3()
    job = await asyncio.to_thread(esc.get_job, w3, job_id)
    if job["status"] != "Funded":
        return {"error": f"job {job_id} is not available (status: {job['status']})"}
    review = await asyncio.to_thread(_committee_review, w3, job_id, job.get("quorum", 1))
    gate = _committee_gate(review, job.get("quorum", 1))
    if isinstance(gate, dict):
        return gate  # hard abort (operator opted into committee vetting)
    salt = esc.random_salt()
    _salts[job_id] = salt
    try:
        tx = await _sign(config.ESCROW_V4_ADDRESS,
                         esc.commit_accept(esc.commitment(job_id, MCP_ADDRESS, salt)), "commitAccept")
    except Exception as e:
        _salts.pop(job_id, None)
        return {"error": f"commitAccept failed: {type(e).__name__}: {e}"}
    return {"job_id": job_id, "committed": True, "tx": _tx(tx), "committee_warning": gate,
            "next": "after ~%d block(s), reveal_accept(%d)" % (config.REVEAL_DELAY_BLOCKS, job_id)}


@mcp.tool()
async def reveal_accept(job_id: int) -> dict:
    """[provider] Step 2 of the sealed race: open your commitment and claim the job. Must follow a
    commit_accept(job_id) in this session (the salt is held here). First valid reveal wins; a late
    reveal reverts. Routed through the private-mempool hook when MEV_PROTECT is enabled."""
    salt = _salts.get(job_id)
    if salt is None:
        return {"error": f"no committed bid for job {job_id} in this session; call commit_accept({job_id}) first"}
    w3 = esc.web3()
    try:
        tx = await _sign(config.ESCROW_V4_ADDRESS, esc.reveal_accept(job_id, salt), "revealAccept",
                         private_tx=config.MEV_PROTECT)
    except Exception as e:
        return {"won": False, "error": f"revealAccept reverted - likely lost the sealed race: {type(e).__name__}: {e}"}
    after = await asyncio.to_thread(esc.get_job, w3, job_id)
    won = int(after["provider"], 16) == int(MCP_ADDRESS, 16)
    _salts.pop(job_id, None)
    return {"job_id": job_id, "won": won, "tx": _tx(tx), "provider_now": after["provider"],
            "next": "deliver_work(%d, <your work>)" % job_id if won else "another provider won; pick another job"}


@mcp.tool()
async def deliver_work(job_id: int, deliverable: str) -> dict:
    """[provider] Store your work on Irys and submit its keccak256 hash on-chain (submitWork). You must be the
    job's accepted provider. The client/evaluator then judges it and settles."""
    w3 = esc.web3()
    job = await asyncio.to_thread(esc.get_job, w3, job_id)
    if job["status"] != "Accepted":
        return {"error": f"job {job_id} is not Accepted (status: {job['status']})"}
    if not deliverable.strip():
        return {"error": "deliverable is empty"}
    dhash = Web3.keccak(text=deliverable)
    try:
        irys = await asyncio.to_thread(
            irys_store.upload, deliverable,
            {"app": "AgentWorks", "job-id": str(job_id), "content-keccak": Web3.to_hex(dhash)})
    except Exception as e:
        return {"error": f"Irys upload failed: {type(e).__name__}: {e}"}
    tx = await _sign(config.ESCROW_V4_ADDRESS, esc.submit_work(job_id, dhash, irys.get("id")), "submitWork")
    return {"job_id": job_id, "irys_id": irys.get("id"), "irys_url": irys.get("url"),
            "deliverable_hash": Web3.to_hex(dhash), "tx": _tx(tx),
            "next": "the committee now votes via cast_vote(%d, approve=...); finalize(%d) after the dispute window" % (job_id, job_id)}


def main() -> None:
    mcp.run()  # stdio transport (Claude Desktop / Code). For HTTP: mcp.run(transport="streamable-http").


if __name__ == "__main__":
    main()
