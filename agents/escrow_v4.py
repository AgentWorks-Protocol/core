"""Escrow v4 (OPEN marketplace, committee consensus + staked disputes) calldata builders + reads.

Supersedes escrow_v3.py: the lone evaluator is replaced by an M-of-N evaluator COMMITTEE that votes,
and settlement of a contested job escalates (with a staked bond) to a DECOUPLED, DECENTRALIZED arbiter
(UMA Optimistic Oracle V3) — never an operator key. The v3 sealed commit-reveal accept is carried over.

v4 lifecycle:
  createJob(evaluators[], quorum, …) → fund → commitAccept → revealAccept → submitWork →
  castVote ×N → Resolved(tentative) → finalize | dispute → (Disputed) resolveDispute | resolveTimeout
  (deadline anti-freeze exit for a stalled job: claimRefund)

Status enum is RENUMBERED vs v3 (adds Resolved=5, Disputed=6; Completed=7) — this module has its own
STATUS map; do not reuse the v3 decoder. See docs/ARBITRATION.md for the threat model + design.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from eth_abi import encode
from web3 import Web3

import config

REPO_ROOT = Path(__file__).resolve().parents[1]
_VENDORED = Path(__file__).resolve().parent / "abi" / "AgentWorksEscrowV4.json"
_ARTIFACT = REPO_ROOT / "contracts" / "out" / "AgentWorksEscrowV4.sol" / "AgentWorksEscrowV4.json"
_abi_path = _VENDORED if _VENDORED.exists() else _ARTIFACT
ESCROW_V4_ABI = json.loads(_abi_path.read_text(encoding="utf-8"))["abi"]

ERC20_ABI = json.loads(
    '[{"name":"balanceOf","type":"function","stateMutability":"view",'
    '"inputs":[{"name":"a","type":"address"}],"outputs":[{"type":"uint256"}]},'
    '{"name":"allowance","type":"function","stateMutability":"view",'
    '"inputs":[{"name":"o","type":"address"},{"name":"s","type":"address"}],"outputs":[{"type":"uint256"}]}]'
)

# v4 status enum (RENUMBERED — adds Resolved + Disputed).
STATUS = {0: "None", 1: "Open", 2: "Funded", 3: "Accepted", 4: "Submitted",
          5: "Resolved", 6: "Disputed", 7: "Completed", 8: "Rejected", 9: "Refunded"}

_cs = Web3.to_checksum_address


def _retry(thunk, tries: int = 5, delay: float = 2.0):
    for i in range(tries):
        try:
            return thunk()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(delay)


def _calldata(signature: str, types: list[str], args: list) -> str:
    selector = Web3.keccak(text=signature)[:4]
    return "0x" + (selector + encode(types, args)).hex()


# ── calldata builders ──

def approve(spender: str, amount: int) -> str:
    return _calldata("approve(address,uint256)", ["address", "uint256"], [_cs(spender), amount])


def create_job(evaluators: list[str], quorum: int, amount: int, spec_hash: bytes, deadline: int) -> str:
    """v4: OPEN job naming an evaluator COMMITTEE (odd N) + a strict-majority quorum."""
    return _calldata(
        "createJob(address[],uint8,uint256,bytes32,uint64)",
        ["address[]", "uint8", "uint256", "bytes32", "uint64"],
        [[_cs(e) for e in evaluators], quorum, amount, spec_hash, deadline],
    )


def fund(job_id: int) -> str:
    return _calldata("fund(uint256)", ["uint256"], [job_id])


# ── sealed commit-reveal accept (carried from v3) ──

def random_salt() -> bytes:
    return os.urandom(32)


def commitment(job_id: int, sender: str, salt: bytes) -> bytes:
    return Web3.keccak(encode(["uint256", "address", "bytes32"], [job_id, _cs(sender), salt]))


def commit_accept(commitment_hash: bytes) -> str:
    return _calldata("commitAccept(bytes32)", ["bytes32"], [commitment_hash])


def reveal_accept(job_id: int, salt: bytes) -> str:
    return _calldata("revealAccept(uint256,bytes32)", ["uint256", "bytes32"], [job_id, salt])


def submit_work(job_id: int, deliverable_hash: bytes, irys_id: str) -> str:
    return _calldata("submitWork(uint256,bytes32,string)", ["uint256", "bytes32", "string"],
                     [job_id, deliverable_hash, irys_id])


# ── committee voting + dispute ──

def cast_vote(job_id: int, approve_vote: bool) -> str:
    return _calldata("castVote(uint256,bool)", ["uint256", "bool"], [job_id, approve_vote])


def force_resolve(job_id: int) -> str:
    return _calldata("forceResolve(uint256)", ["uint256"], [job_id])


def finalize(job_id: int) -> str:
    return _calldata("finalize(uint256)", ["uint256"], [job_id])


def dispute(job_id: int) -> str:
    return _calldata("dispute(uint256)", ["uint256"], [job_id])


def resolve_timeout(job_id: int) -> str:
    return _calldata("resolveTimeout(uint256)", ["uint256"], [job_id])


def claim_refund(job_id: int) -> str:
    """Client deadline-refund: the anti-freeze exit for a job that stalled before settlement (Funded but
    never accepted, or Accepted/Submitted but never resolved) once its deadline passes. forceResolve /
    resolveTimeout only cover Submitted / Disputed, so without this a stranded Funded/Accepted job's
    escrow could only be recovered by hand-rolled calldata (see contract claimRefund(uint256))."""
    return _calldata("claimRefund(uint256)", ["uint256"], [job_id])


# ── on-chain reads ──

def _retrying_session():
    """A requests session that retries transient RPC failures (dropped connections, 429/5xx). All web3
    POSTs here are reads (eth_call / eth_blockNumber / eth_getTransactionReceipt) — idempotent, so safe to
    retry — and a single blip on a flaky network must not crash a multi-minute hands-off run. Txs go
    through CAW, not web3, so we never retry a send here."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    retry = Retry(total=5, connect=5, read=5, backoff_factor=0.5,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "POST"]))
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def web3() -> Web3:
    url = config.PRIVATE_RPC_URL or config.RPC_URL
    return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}, session=_retrying_session()))


def _escrow(w3: Web3):
    return w3.eth.contract(address=_cs(config.ESCROW_V4_ADDRESS), abi=ESCROW_V4_ABI)


def next_job_id(w3: Web3) -> int:
    return _retry(lambda: _escrow(w3).functions.nextJobId().call())


def get_job(w3: Web3, job_id: int) -> dict:
    j = _retry(lambda: _escrow(w3).functions.getJob(job_id).call())
    return {
        "client": j[0], "provider": j[1], "amount": j[2],
        "spec_hash": Web3.to_hex(j[3]), "deliverable_hash": Web3.to_hex(j[4]),
        "irys_id": j[5], "deadline": j[6], "status": STATUS.get(j[7], j[7]),
        "committee_size": j[8], "quorum": j[9],
    }


def get_committee(w3: Web3, job_id: int) -> list[str]:
    return _retry(lambda: _escrow(w3).functions.getCommittee(job_id).call())


def get_vote(w3: Web3, job_id: int) -> dict:
    v = _retry(lambda: _escrow(w3).functions.getVote(job_id).call())
    return {"approve": v[0], "reject": v[1], "voting_deadline_block": v[2],
            "tentative_payout": v[3], "resolved_block": v[4]}


def get_dispute(w3: Web3, job_id: int) -> dict:
    d = _retry(lambda: _escrow(w3).functions.getDispute(job_id).call())
    return {"disputer": d[0], "dispute_block": d[1]}


def has_member_voted(w3: Web3, job_id: int, member: str) -> bool:
    return _retry(lambda: _escrow(w3).functions.hasMemberVoted(job_id, _cs(member)).call())


def usdc_balance(w3: Web3, addr: str) -> int:
    t = w3.eth.contract(address=_cs(config.USDC_ADDRESS), abi=ERC20_ABI)
    return _retry(lambda: t.functions.balanceOf(_cs(addr)).call())


def usdc_allowance(w3: Web3, owner: str, spender: str) -> int:
    t = w3.eth.contract(address=_cs(config.USDC_ADDRESS), abi=ERC20_ABI)
    return _retry(lambda: t.functions.allowance(_cs(owner), _cs(spender)).call())
