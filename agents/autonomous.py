"""Autonomous agents - continuous loops over the open marketplace (Phase 6.5.3).

Roles, each acting through its own CAW wallet under a scoped Pact, coordinating via an off-chain job
board (the marketplace listing) + the on-chain v4 escrow (the source of truth):

  Client loop    - for each task it deems worth funding: createJob (OPEN, naming an evaluator
                   COMMITTEE) → approve → fund, posts the task. Then it FINALIZES: once the committee
                   has reached a tentative outcome (Resolved) and the dispute window elapses with no
                   dispute, it calls finalize() to execute the payout/refund.
  Provider pool  - N provider identities. Each runs the SEALED commit-reveal race (commitAccept →
                   revealAccept; first valid reveal wins), then does the work, stores on Irys, submitWork()s.
  Committee pool - M-of-N evaluators. Each independently pulls the deliverable from Irys, judges it
                   (distinct LLM personas), and castVote()s. Reaching quorum resolves the job
                   tentatively (no funds move). A contested outcome escalates (staked) to the
                   decoupled, decentralized arbiter (UMA OOv3) — never an operator key.

Genuine LLM reasoning at every decision (criterion 1); the Pact is the hard boundary regardless
(criterion 2). Every CAW call + decision is logged; a proof artifact is written per settled job.
Reuses escrow_v4 / pacts / reasoning / registry / irys_store - invents no SDK surface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from uuid import uuid4

import config
import escrow_v4 as esc
import irys_store
import pacts
import reasoning
import registry
from caw import CawWallet
from web3 import Web3

log = logging.getLogger("auto")

# Off-chain marketplace state (board + run artifacts). On a host with ephemeral storage (e.g. Railway),
# set AGENT_DATA_DIR to a mounted volume so listings + registrations survive restarts; default is the
# in-repo path for local dev.
_DATA_DIR = Path(os.environ["AGENT_DATA_DIR"]) if os.environ.get("AGENT_DATA_DIR") else (Path(__file__).resolve().parent / "scripts")
MARKET_DIR = _DATA_DIR / ".market"
BOARD_FILE = MARKET_DIR / "board.json"
RUNS_DIR = MARKET_DIR / "runs"
POLL = 4.0  # seconds between scans


# ── off-chain job board (the marketplace listing) ───────────────────────────

def _read_board() -> dict:
    if BOARD_FILE.exists():
        try:
            return json.loads(BOARD_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _write_board(board: dict) -> None:
    MARKET_DIR.mkdir(parents=True, exist_ok=True)
    BOARD_FILE.write_text(json.dumps(board, indent=2), encoding="utf-8")


def _post_listing(job_id: int, *, task: str, criteria: str, reward_usdc: float,
                  spec_hash: str, client: str, deadline: int, salt: str = "",
                  spec_irys_id: str = "") -> None:
    board = _read_board()
    board[str(job_id)] = {
        "job_id": job_id, "task": task, "criteria": criteria, "reward_usdc": reward_usdc,
        "spec_hash": spec_hash, "client": client, "deadline": deadline, "posted_at": int(time.time()),
        "salt": salt, "spec_irys_id": spec_irys_id,
    }
    _write_board(board)


def _listing(job_id: int) -> dict | None:
    return _read_board().get(str(job_id))


def _spec_text(listing: dict) -> str:
    crit = (listing.get("criteria") or "").strip()
    return f"{listing['task']}\n\nAcceptance criteria: {crit}" if crit else listing["task"]


def _spec_for(job_id: int, job: dict) -> str | None:
    """The task text for a CHAIN-discovered job. Primary source is this operator's board listing; if that's
    missing the text but recorded a `spec_irys_id`, fetch the spec from Irys and hash-verify it against the
    on-chain specHash (salt-bound) before trusting it. Returns None when no spec is discoverable here —
    e.g. a job funded through a different operator's board (trustless cross-operator discovery needs the
    on-chain spec pointer, a v5/Base item)."""
    listing = _listing(job_id)
    if listing and (listing.get("task") or "").strip():
        return _spec_text(listing)
    sid = (listing or {}).get("spec_irys_id")
    if sid:
        try:
            raw = irys_store.fetch(sid).decode("utf-8", "replace")
            salt = (listing or {}).get("salt", "")
            if Web3.to_hex(Web3.keccak(text=f"{raw}#{salt}")).lower() == str(job.get("spec_hash", "")).lower():
                return raw
        except Exception:
            return None
    return None


def _committee_trusted(w3, job_id: int, quorum: int) -> bool:
    """True iff the job's on-chain committee contains at least `quorum` PLATFORM-OPERATED evaluators — i.e.
    trusted members alone can reach quorum and settle honestly. Defends a provider against a client that
    named a sock-puppet committee to reject delivered work and refund itself (the free-work attack)."""
    try:
        members = esc.get_committee(w3, job_id)
    except Exception:
        return False
    trusted = registry.trusted_evaluator_addrs()
    known = sum(1 for m in members if m.lower() in trusted)
    return known >= max(1, quorum)


def _adopt(run: "Run", job_id: int) -> dict:
    """Ensure a run record exists for job_id, seeding task/criteria/amount/client from the board listing
    so a FIRST-PARTY worker can act on a job this run didn't itself create (the open market). Committee is
    read live from the chain by the committee worker (source of truth), not seeded here."""
    rec = run.record(job_id)
    if not rec.get("task"):
        listing = _listing(job_id)
        if listing:
            rec.update({"task": listing.get("task"), "criteria": listing.get("criteria", ""),
                        "amount_usdc": listing.get("reward_usdc"), "client": listing.get("client")})
    return rec


# ── shared run state ────────────────────────────────────────────────────────

class Run:
    def __init__(self, *, mode: str, target: int) -> None:
        self.mode = mode                      # 'good' | 'bad'
        self.target = target                  # stop after this many jobs settle
        self.run_id = uuid4().hex[:10]
        self.jobs: dict[int, dict] = {}       # job_id -> record
        self.settled = 0
        self.stop = asyncio.Event()

    def record(self, job_id: int) -> dict:
        return self.jobs.setdefault(job_id, {
            "run_id": self.run_id, "job_id": job_id, "txs": {}, "accept_decisions": {},
            "winner": None, "winner_addr": None, "irys": None, "deliverable": None,
            "committee": [], "committee_votes": {}, "vote_txs": {}, "tentative": None,
            "verdict": None, "branch": None, "status": "open",
        })

    def write_artifact(self, job_id: int) -> None:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        rec = self.jobs[job_id]
        (RUNS_DIR / f"{job_id}.json").write_text(json.dumps(rec, default=str, indent=2), encoding="utf-8")


async def _revoke_active(w: CawWallet) -> None:
    try:
        page = await w.list_pacts(status="active")
    except Exception:
        return
    items = page if isinstance(page, list) else (page.get("items", []) if isinstance(page, dict) else [])
    for p in items:
        if isinstance(p, dict) and p.get("status") == "active" and p.get("id"):
            try:
                await w.revoke_pact(p["id"])
            except Exception:
                pass


async def _call(agent: CawWallet, src: str, target: str, calldata: str, label: str,
                *, private_tx: bool = False) -> str:
    rid = f"auto-{uuid4().hex[:10]}"
    resp = await agent.contract_call(src_addr=src, contract_addr=target, calldata=calldata,
                                     chain_id=config.CHAIN_ID, request_id=rid, description=label,
                                     private_tx=private_tx)
    # Generous timeout: CAW's TSS relay can drop + re-register over a ~3-min window, during which a
    # signature stalls at status 400 "signing" before completing. 420s outlasts that reconnect window.
    rec = await agent.wait_tx_final(rid, timeout=420.0)
    return (rec or {}).get("transaction_hash") or (resp or {}).get("transaction_hash") or ""


async def _commit_block(w3: Web3, tx_hash: str) -> int:
    """Block number a commit tx landed in, so we can wait out the reveal delay. Falls back to the
    current head if the receipt isn't fetchable (the reveal delay is then satisfied by CAW latency)."""
    if not tx_hash:
        return await asyncio.to_thread(lambda: w3.eth.block_number)
    try:
        rcpt = await asyncio.to_thread(w3.eth.get_transaction_receipt, tx_hash)
        return int(rcpt["blockNumber"])
    except Exception:
        return await asyncio.to_thread(lambda: w3.eth.block_number)


async def _wait_reveal_ready(w3: Web3, commit_block: int) -> None:
    """Block until block.number >= commit_block + REVEAL_DELAY_BLOCKS. CAW's multi-minute relay
    almost always clears this already, but we wait defensively so a reveal never lands too early."""
    ready = commit_block + config.REVEAL_DELAY_BLOCKS
    for _ in range(120):  # ~ up to a few minutes of 12s blocks
        head = await asyncio.to_thread(lambda: w3.eth.block_number)
        if head >= ready:
            return
        await asyncio.sleep(POLL)


async def _load_deliverable(w3: Web3, rec: dict, job: dict) -> str | None:
    """The deliverable bytes the committee judges (and the client content-verifies). Primary source is
    Irys, the canonical store. If the gateway is transiently unreachable (e.g. a TLS-inspecting local
    proxy), fall back to the run-record copy — but ONLY when its keccak equals the on-chain
    `deliverableHash`, so we never judge or settle on unauthenticated content (the on-chain hash is the
    trust anchor; the source of the bytes is not). Returns None if neither yields hash-authentic content."""
    onchain = (job.get("deliverable_hash") or "").lower()
    # Prefer the run-record irys id; fall back to the ON-CHAIN irys id so the committee can judge an
    # externally-submitted job the run never tracked (submitWork anchors the id on-chain).
    irys_id = (rec.get("irys") or {}).get("id") or job.get("irys_id")
    if irys_id:
        try:
            raw = await asyncio.to_thread(irys_store.fetch, irys_id)
            if not onchain or irys_store.keccak(raw).lower() == onchain:
                return raw.decode("utf-8", "replace")
            log.warning("[deliverable] Irys content hash != on-chain anchor for job; rejecting")
        except Exception as e:  # noqa: BLE001 - gateway/TLS hiccup; try the hash-verified local copy
            log.info("[deliverable] Irys fetch failed (%s); trying hash-verified run-record copy",
                     type(e).__name__)
    copy = rec.get("deliverable")
    if copy is not None and onchain and irys_store.keccak(copy.encode("utf-8")).lower() == onchain:
        log.info("[deliverable] using hash-verified run-record copy (keccak == on-chain anchor)")
        return copy
    return None


# ── provider worker (one per provider ADDRESS; bound to ITS OWN wallet's pact) ──

async def provider_worker(run: Run, name: str, addr: str, api_key: str, wallet_id: str, pact: dict) -> None:
    """Continuously discover open jobs, reason about each, and race the sealed accept for the ones worth it,
    then deliver. Signs from the provider's OWN wallet (api_key/wallet_id) — so a separate-wallet or custodial
    provider is driven correctly, not silently dropped. The reward it reasons over is the job's own listing."""
    w3 = esc.web3()
    attempted: set[int] = set()
    async with CawWallet(api_url=config.CAW_API_URL, api_key=api_key,
                         wallet_uuid=wallet_id, name=name) as pw_root:
        async with pw_root.scoped(pact, name_suffix="") as pw:
            while not run.stop.is_set():
                # Chain-authoritative discovery: the chain (not the local board) is the source of truth for
                # which jobs exist + their state. Sweep recent Funded, unclaimed ids; the task text comes from
                # the board listing (or an Irys fallback), hash-bound to the on-chain specHash at post time.
                try:
                    candidates = await asyncio.to_thread(
                        lambda: list(esc.scan_jobs(w3, statuses={"Funded"}, skip=attempted)))
                except Exception:
                    candidates = []
                for job_id, job in candidates:
                    if int(job["provider"], 16) != 0:
                        attempted.add(job_id)  # already claimed by someone — stop rechecking
                        continue
                    rec = _adopt(run, job_id)  # adopt ANY open job, not just this run's (dissolve the pool)
                    if rec.get("winner"):
                        continue
                    spec = await asyncio.to_thread(_spec_for, job_id, job)
                    if not spec:
                        continue  # no spec discoverable on this operator (e.g. posted via a different board)
                    reward = float((_listing(job_id) or {}).get("reward_usdc") or (job["amount"] / 1_000_000))
                    # Claim-policy gates (before spending any reasoning or gas):
                    if config.PROVIDER_MIN_REWARD_USDC and reward < config.PROVIDER_MIN_REWARD_USDC:
                        attempted.add(job_id)
                        continue  # below the reward floor — a bounded participant ignores dust
                    if reward > config.MAX_JOB_REWARD_USDC:
                        attempted.add(job_id)
                        log.info("[%s] skipping job #%s: reward %.2f USDC exceeds the per-job ceiling %.2f "
                                 "(real-money anomaly guard)", name, job_id, reward, config.MAX_JOB_REWARD_USDC)
                        continue  # above the sane per-job ceiling — a job that shouldn't exist on our rails
                    if config.PROVIDER_REQUIRE_KNOWN_COMMITTEE and not await asyncio.to_thread(
                            _committee_trusted, w3, job_id, job.get("quorum", 1)):
                        attempted.add(job_id)
                        log.info("[%s] skipping job #%s: committee not sufficiently platform-operated "
                                 "(anti free-work vetting)", name, job_id)
                        continue
                    decision = await asyncio.to_thread(reasoning.provider_decide_accept, spec, reward,
                                                       provider_name=name)
                    rec["accept_decisions"][name] = decision
                    if not decision.get("accept"):
                        attempted.add(job_id)
                        continue
                    attempted.add(job_id)
                    # SEALED ACCEPT RACE (v3 commit-reveal): step 1 publishes only an opaque hash
                    # binding (jobId, addr, salt) - the public mempool learns neither which job nor
                    # anything reusable. The jobId stays hidden until reveal, defeating the frontrun.
                    salt = esc.random_salt()
                    commitment = esc.commitment(job_id, addr, salt)
                    try:
                        commit_tx = await _call(pw, addr, config.ESCROW_V4_ADDRESS,
                            esc.commit_accept(commitment), f"commitAccept[{name}]")
                    except Exception as e:
                        log.info("[%s] commitAccept for job #%s failed (%s)", name, job_id, type(e).__name__)
                        continue
                    rec.setdefault("commits", {})[name] = {"addr": addr, "tx": commit_tx}
                    run.write_artifact(job_id)
                    log.info("[%s] committed sealed bid for job #%s -> %s", name, job_id, commit_tx)

                    # step 2: after the reveal delay, open the bid. The FIRST valid reveal wins; a
                    # loser's reveal reverts (job no longer Funded). Route the reveal through the
                    # private-mempool hook when MEV_PROTECT is on (defense-in-depth on the residual).
                    cblock = await _commit_block(w3, commit_tx)
                    await _wait_reveal_ready(w3, cblock)
                    try:
                        reveal_tx = await _call(pw, addr, config.ESCROW_V4_ADDRESS,
                            esc.reveal_accept(job_id, salt), f"revealAccept[{name}]",
                            private_tx=config.MEV_PROTECT)
                    except Exception as e:
                        log.info("[%s] lost the sealed race for job #%s (%s)", name, job_id, type(e).__name__)
                        rec.setdefault("race_losers", []).append({"name": name, "addr": addr, "error": type(e).__name__})
                        run.write_artifact(job_id)
                        continue
                    # double-check we actually hold it (race-safe)
                    job = await asyncio.to_thread(esc.get_job, w3, job_id)
                    if int(job["provider"], 16) != int(addr, 16):
                        log.info("[%s] revealAccept for #%s did not stick; winner=%s", name, job_id, job["provider"])
                        continue
                    rec["winner"], rec["winner_addr"] = name, addr
                    rec["txs"]["commitAccept"] = commit_tx
                    rec["txs"]["revealAccept"] = reveal_tx
                    rec["provider"] = addr
                    run.write_artifact(job_id)
                    log.info("[%s] WON job #%s -> revealAccept %s", name, job_id, reveal_tx)

                    # do the work, store on Irys, submit
                    deliverable = await asyncio.to_thread(reasoning.provider_do_task, spec)
                    rec["deliverable"] = deliverable
                    dhash = Web3.keccak(text=deliverable)
                    irys = await asyncio.to_thread(irys_store.upload, deliverable,
                        {"app": "AgentWorks", "job-id": str(job_id), "content-keccak": Web3.to_hex(dhash)})
                    rec["irys"] = irys
                    rec["txs"]["submitWork"] = await _call(pw, addr, config.ESCROW_V4_ADDRESS,
                        esc.submit_work(job_id, dhash, irys["id"]), f"submitWork[{name}]")
                    rec["status"] = "submitted"
                    run.write_artifact(job_id)
                    log.info("[%s] submitted work for job #%s (Irys %s)", name, job_id, irys["id"])
                await asyncio.sleep(POLL)


# ── committee worker (one per evaluator identity; share the evaluator wallet's pact) ──

async def committee_worker(run: Run, member: "registry.Participant", vote_lock: asyncio.Lock) -> None:
    """One committee member, independent on BOTH axes: it signs from its OWN CAW wallet (its own Pact +
    TSS node) and reasons on its OWN model (`member.llm()`). It scans for Submitted jobs it's on the
    committee for + hasn't voted on, pulls the deliverable from Irys, judges it, and castVotes on-chain.
    Reaching quorum tentatively resolves the job (the contract enforces this; no funds move).

    Casts are serialized across the committee via `vote_lock`: the quorum-reaching vote triggers the
    contract's `_resolve` (extra SSTOREs + event), so it needs more gas than a plain vote. If members
    cast concurrently, CAW estimates each against the cheap pre-quorum state and the resolving vote
    reverts out-of-gas. Voting one-at-a-time makes each cast estimate against current chain state."""
    name, addr, mllm = member.name, member.address, member.llm()
    w3 = esc.web3()
    voted: set[int] = set()
    async with CawWallet(api_url=config.CAW_API_URL, api_key=member.api_key,
                         wallet_uuid=member.wallet_id, name=name) as ew_root:
        await _revoke_active(ew_root)  # each member onboards its OWN evaluator pact (castVote-only, no USDC)
        sub = await ew_root.submit_pact(intent=f"{name} votes on marketplace deliverables",
                                        spec=pacts.evaluator_pact(escrow=config.ESCROW_V4_ADDRESS),
                                        name=f"auto-{name.replace(' ', '')}-{run.run_id}")
        pact = await ew_root.wait_pact_active(sub.get("pact_id"))
        log.info("[%s] onboarded (wallet %s…, model %s)", name, member.wallet_id[:8], mllm["model"])
        async with ew_root.scoped(pact, name_suffix="") as ew:
            while not run.stop.is_set():
                # Chain-authoritative: sweep recent Submitted jobs. The committee is fixed on-chain, so we act
                # only where our address is a member. Criteria come from the board listing (or an Irys
                # fallback), hash-bound to the on-chain specHash; the deliverable is fetched from Irys.
                try:
                    candidates = await asyncio.to_thread(
                        lambda: list(esc.scan_jobs(w3, statuses={"Submitted"}, skip=voted)))
                except Exception:
                    candidates = []
                for job_id, job in candidates:
                    rec = _adopt(run, job_id)  # adopt ANY job we're on the committee for (dissolve the pool)
                    try:
                        members = await asyncio.to_thread(esc.get_committee, w3, job_id)
                    except Exception:
                        continue
                    if int(addr, 16) not in (int(a, 16) for a in members):
                        voted.add(job_id)  # not on this job's committee (fixed on-chain) — skip henceforth
                        continue
                    if await asyncio.to_thread(esc.has_member_voted, w3, job_id, addr):
                        voted.add(job_id)
                        continue
                    if not (rec.get("irys") or job.get("irys_id")):
                        continue  # deliverable not stored yet (check on-chain irys id for external jobs)
                    spec = _spec_for(job_id, job)
                    if not spec:
                        continue  # acceptance criteria not discoverable here yet — retry next poll
                    try:
                        fetched = await _load_deliverable(w3, rec, job)
                        if fetched is None:
                            continue  # deliverable unfetchable right now — retry next poll
                        verdict = await asyncio.to_thread(reasoning.evaluate_member,
                                                          spec, fetched, member_name=name, llm=mllm)
                        approve = bool(verdict.get("accept"))
                    except Exception as e:  # transient model/network hiccup (503/429/timeout) — retry next poll
                        log.info("[%s] evaluate job #%s failed (%s); retrying next poll", name, job_id, type(e).__name__)
                        continue
                    async with vote_lock:  # serialize casts: correct gas for the quorum-reaching vote
                        try:
                            # a peer may have reached quorum + Resolved the job while we judged
                            jnow = await asyncio.to_thread(esc.get_job, w3, job_id)
                            if jnow["status"] != "Submitted":
                                voted.add(job_id)
                                continue
                            tx = await _call(ew, addr, config.ESCROW_V4_ADDRESS, esc.cast_vote(job_id, approve),
                                             f"castVote[{name}]")
                        except Exception as e:
                            log.info("[%s] castVote for job #%s failed (%s) - likely quorum already reached",
                                     name, job_id, type(e).__name__)
                            voted.add(job_id)
                            continue
                    voted.add(job_id)
                    rec.setdefault("committee_votes", {})[name] = {"addr": addr, **verdict}
                    rec.setdefault("vote_txs", {})[name] = tx
                    run.write_artifact(job_id)
                    log.info("[%s] voted %s on job #%s -> %s", name, "ACCEPT" if approve else "REJECT", job_id, tx)
                await asyncio.sleep(POLL)


# ── standing platform services (the platform's own agents participate continuously) ──

async def provider_service(run: Run) -> None:
    """Standing provider participation: for each AUTO-DRIVABLE provider WALLET, onboard its provider Pact once
    and run one worker per address on it. Workers chain-discover ANY open job and race the sealed accept, so a
    separate-wallet (CAW_PROVIDER2_*) or custodial provider is driven too — not silently dropped. Keyless
    (self-driven) providers are excluded here; they act via the calldata rail / MCP against the same jobs."""
    by_wallet: dict[str, list[registry.Participant]] = {}
    for p in registry.providers():
        if p.is_self_driven:
            continue
        by_wallet.setdefault(p.wallet_id, []).append(p)
    if not by_wallet:
        log.info("[provider-svc] no auto-drivable providers configured; skipping")
        return
    workers = []
    for wallet_id, members in by_wallet.items():
        try:
            async with CawWallet(api_url=config.CAW_API_URL, api_key=members[0].api_key,
                                 wallet_uuid=wallet_id, name=members[0].name) as root:
                await _revoke_active(root)
                sub = await root.submit_pact(intent="Providers accept + deliver marketplace jobs",
                    spec=pacts.provider_pact(escrow=config.ESCROW_V4_ADDRESS),
                    name=f"platform-provider-{wallet_id[:8]}")
                pact = await root.wait_pact_active(sub.get("pact_id"))
        except Exception as e:  # noqa: BLE001 - a bad wallet shouldn't take down the whole service
            log.warning("[provider-svc] onboard wallet %s… failed (%s); skipping", wallet_id[:8], type(e).__name__)
            continue
        for m in members:
            workers.append(provider_worker(run, m.name, m.address, m.api_key, m.wallet_id, pact))
    log.info("[provider-svc] %d provider worker(s) live", len(workers))
    if workers:
        await asyncio.gather(*workers)


async def committee_service(run: Run) -> None:
    """Standing committee participation: one worker per AUTO-DRIVABLE evaluator. Each self-onboards its own
    castVote-only Pact and votes on any Submitted job whose on-chain committee names it. Keyless evaluators
    self-vote via the calldata rail and are excluded here."""
    members = [m for m in registry.evaluators() if not m.is_self_driven]
    if not members:
        log.info("[committee-svc] no auto-drivable evaluators configured; skipping")
        return
    vote_lock = asyncio.Lock()  # serialize casts: correct gas for the quorum-reaching vote
    log.info("[committee-svc] %d committee worker(s) live: %s", len(members),
             [(m.name, m.address[:8], m.llm()["model"]) for m in members])
    await asyncio.gather(*[committee_worker(run, m, vote_lock) for m in members])


async def platform_agents(stop: asyncio.Event | None = None) -> None:
    """Run the platform's own provider + committee as STANDING participants in the open marketplace — they
    claim + settle jobs posted by anyone (external clients via calldata/MCP, or the operator), not just a demo
    run. Gated by PLATFORM_AGENTS + wallet presence. Settlement/finalize is the separate settlement_watcher."""
    run = Run(mode="live", target=10**9)
    if stop is not None:
        run.stop = stop
    log.info("[platform] standing provider + committee services starting")
    await asyncio.gather(provider_service(run), committee_service(run))


# ── settlement watcher (standing liveness; settles EXTERNAL jobs the platform doesn't drive) ──

def has_first_party_signer() -> bool:
    """True if a wallet is configured to sign the (permissionless) settlement steps."""
    try:
        config.watcher_agent()
        return True
    except Exception:
        return False


async def _settlement_sweep(w3: Web3, signer, src_addr: str, *, max_scan: int = 60) -> None:
    """One pass: execute the PERMISSIONLESS settlement step each recent job is due for — finalize (Resolved
    past dispute window), forceResolve (stalled Submitted), resolveTimeout (Disputed, arbiter silent). Every
    step is outcome-NEUTRAL: it executes the committee's already-decided result or the contract's anti-freeze
    rule, and any address may call it — so this is liveness, not an operator ruling. (claimRefund is NOT here:
    the contract restricts it to the job's client, so an external client reclaims via /refund-calldata.)
    Reverts — someone else settled first, or the window isn't truly elapsed — are expected and skipped."""
    try:
        n = await asyncio.to_thread(esc.next_job_id, w3)
    except Exception:
        return
    head = await asyncio.to_thread(lambda: w3.eth.block_number)
    for jid in range(max(1, n - max_scan), n):
        try:
            job = await asyncio.to_thread(esc.get_job, w3, jid)
        except Exception:
            continue
        st = job["status"]
        try:
            if st == "Resolved":
                v = await asyncio.to_thread(esc.get_vote, w3, jid)
                if head > int(v["resolved_block"]) + config.DISPUTE_WINDOW_BLOCKS:
                    await _call(signer, src_addr, config.ESCROW_V4_ADDRESS, esc.finalize(jid), "watcher.finalize")
                    log.info("[watcher] finalized job #%s (dispute window elapsed)", jid)
            elif st == "Submitted":
                v = await asyncio.to_thread(esc.get_vote, w3, jid)
                if v["voting_deadline_block"] and head > int(v["voting_deadline_block"]):
                    await _call(signer, src_addr, config.ESCROW_V4_ADDRESS, esc.force_resolve(jid),
                                "watcher.forceResolve")
                    log.info("[watcher] forceResolved stalled job #%s (voting window elapsed, no quorum)", jid)
            elif st == "Disputed":
                d = await asyncio.to_thread(esc.get_dispute, w3, jid)
                if head > int(d["dispute_block"]) + config.DISPUTE_RESOLVE_WINDOW_BLOCKS:
                    await _call(signer, src_addr, config.ESCROW_V4_ADDRESS, esc.resolve_timeout(jid),
                                "watcher.resolveTimeout")
                    log.info("[watcher] resolveTimeout'd job #%s (arbiter silent past resolve window)", jid)
            # NB: expired Funded/Accepted jobs are NOT refunded here — claimRefund is client-only on-chain
            # (msg.sender == job.client), so it would always revert for external jobs. The client reclaims
            # via GET /marketplace/jobs/{id}/refund-calldata.
        except Exception as e:  # revert or transient — someone else settled, or the window isn't truly up
            log.debug("[watcher] job #%s (%s) step skipped (%s)", jid, st, type(e).__name__)


async def settlement_watcher(*, poll: float = 15.0, stop: asyncio.Event | None = None) -> None:
    """Standing settlement-liveness loop, independent of any triggered run. Ensures EXTERNAL jobs (posted,
    claimed, and voted by agents the platform doesn't drive) never hang: it finalizes a Resolved job past its
    dispute window, forceResolves a stalled Submitted job, resolveTimeouts a Disputed job the arbiter left
    silent, and claimRefunds an expired unclaimed one. It signs with a FIRST-PARTY wallet under a
    finalize-capable (escrow-allowlisted) Pact; if none is configured the loop no-ops and settlement falls to
    the external client's GET /marketplace/jobs/{id}/finalize-calldata."""
    stop = stop or asyncio.Event()
    if not has_first_party_signer():
        log.info("[watcher] no signer configured; external jobs settle via finalize-calldata")
        return
    cc = config.watcher_agent()  # DEDICATED wallet (CAW_WATCHER_*, else client) so a client op can't revoke it
    w3 = esc.web3()
    log.info("[watcher] settlement watcher started (signer %s…, poll %ss)", cc.wallet_id[:8], poll)
    while not stop.is_set():
        try:
            async with CawWallet(api_url=config.CAW_API_URL, api_key=cc.api_key,
                                 wallet_uuid=cc.wallet_id, name="Watcher") as root:
                sub = await root.submit_pact(
                    intent="Settlement watcher executes permissionless marketplace settlement",
                    spec=pacts.client_escrow_pact(escrow=config.ESCROW_V4_ADDRESS, usdc=config.USDC_ADDRESS),
                    name="settlement-watcher")
                pact = await root.wait_pact_active(sub.get("pact_id"))
                async with root.scoped(pact) as signer:
                    while not stop.is_set():
                        await _settlement_sweep(w3, signer, cc.address)
                        await asyncio.sleep(poll)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pact revoked by a concurrent run / relay hiccup — rebind next cycle
            log.info("[watcher] signer/loop error (%s); rebinding in %ss", type(e).__name__, poll)
            await asyncio.sleep(poll)


def main() -> None:
    """Run the platform's own agents as STANDING participants (provider + committee) plus the settlement
    watcher — the same services the deployed agent service launches at startup. Clients post jobs via their own
    wallet (MCP `post_job` / the calldata rail), not from here."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    argparse.ArgumentParser(description="AgentWorks standing platform agents").parse_args()

    async def _run() -> None:
        stop = asyncio.Event()
        await asyncio.gather(platform_agents(stop), settlement_watcher(stop=stop))

    asyncio.run(_run())


if __name__ == "__main__":
    main()
