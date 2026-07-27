"""Scoped CAW Pact specs - the literal risk-boundary policy (judging criterion 5).

These are the authority boundaries each agent operates within. CAW enforces them server-side;
nothing the agent (or a prompt-injection) does can exceed them. Rendered JSON is written to
docs/pacts/ as first-class deliverables for the risk-boundary doc.

Verified spec shape (docs/FACTS.md): effect "allow" only; block via deny_if; when.target_in is the
contract allowlist; deny_if.usage_limits.rolling_24h.tx_count_gt caps tx count; deny_if.amount_gt
caps transfer amount.
"""

from __future__ import annotations

import json
from pathlib import Path

import config

CHAIN = config.CHAIN_ID
REPO_ROOT = Path(__file__).resolve().parents[1]


def _completion() -> list[dict]:
    return [{"type": "time_elapsed", "threshold": "86400"}]


def client_escrow_pact(escrow: str | None = None, usdc: str | None = None, tx_cap: int = 24) -> dict:
    """Client may ONLY contract_call the escrow + USDC contracts, capped at `tx_cap` tx/24h.

    Parameterized template (Phase 6.5): the marketplace binds the v2 escrow by passing
    `escrow=config.ESCROW_V2_ADDRESS`. No-arg default stays the v1 escrow so the existing v1
    live journey (flow.py) is unaffected.

    REAL-MONEY NOTE (Week-4 safety pass): CAW enforces two dimensions on this contract_call pact —
    the contract ALLOWLIST (escrow + USDC only) and the rolling-24h TX COUNT (both proven on-chain:
    CONTRACT_NOT_WHITELISTED / TRANSFER_LIMIT_EXCEEDED). It does NOT cap the USDC *value*: CAW amount
    limits are transfer-type, and the escrow moves USDC via approve()+fund() (contract calls that pull
    through transferFrom), not `caw tx transfer`. The per-job USDC value ceiling is therefore enforced
    at the marketplace layer where our code sets the amount — server.marketplace_post_calldata /
    mcp_server.post_job (config.MAX_JOB_REWARD_USDC) — backed by conservative platform-wallet funding.
    """
    escrow = escrow or config.ESCROW_ADDRESS
    usdc = usdc or config.USDC_ADDRESS
    return {
        "policies": [{
            "name": "client-escrow-allowlist",
            "type": "contract_call",
            "rules": {
                "effect": "allow",
                "when": {"chain_in": [CHAIN], "target_in": [
                    {"chain_id": CHAIN, "contract_addr": escrow},
                    {"chain_id": CHAIN, "contract_addr": usdc},
                ]},
                "deny_if": {"usage_limits": {"rolling_24h": {"tx_count_gt": tx_cap}}},
            },
        }],
        "completion_conditions": _completion(),
    }


def provider_pact(escrow: str | None = None, tx_cap: int = 16) -> dict:
    """Provider may ONLY contract_call the escrow contract, capped at `tx_cap` tx/24h.

    Parameterized template (Phase 6.5): pass `escrow=config.ESCROW_V2_ADDRESS` for the marketplace.
    Note the provider's allowlist excludes USDC - a provider can `commitAccept`/`revealAccept`/
    `submitWork` but can NEVER move the escrowed funds. That asymmetry is the security-isolation
    evidence (criterion 5).
    """
    escrow = escrow or config.ESCROW_ADDRESS
    return {
        "policies": [{
            "name": "provider-escrow-allowlist",
            "type": "contract_call",
            "rules": {
                "effect": "allow",
                "when": {"chain_in": [CHAIN], "target_in": [
                    {"chain_id": CHAIN, "contract_addr": escrow},
                ]},
                "deny_if": {"usage_limits": {"rolling_24h": {"tx_count_gt": tx_cap}}},
            },
        }],
        "completion_conditions": _completion(),
    }


def evaluator_pact(escrow: str | None = None, tx_cap: int = 16) -> dict:
    """Evaluator (committee member) may ONLY contract_call the escrow — to `castVote` — capped at
    `tx_cap` tx/24h. Like the provider Pact, the evaluator allowlist EXCLUDES USDC: a committee member
    can vote on a deliverable but can NEVER move the escrowed funds. Settlement is the contract's
    (after quorum + the dispute window), never an evaluator's.
    """
    escrow = escrow or config.ESCROW_V4_ADDRESS
    return {
        "policies": [{
            "name": "evaluator-escrow-allowlist",
            "type": "contract_call",
            "rules": {
                "effect": "allow",
                "when": {"chain_in": [CHAIN], "target_in": [
                    {"chain_id": CHAIN, "contract_addr": escrow},
                ]},
                "deny_if": {"usage_limits": {"rolling_24h": {"tx_count_gt": tx_cap}}},
            },
        }],
        "completion_conditions": _completion(),
    }


def disputer_pact(escrow: str | None = None, bond_currency: str | None = None, tx_cap: int = 8) -> dict:
    """A party that escalates a disputed outcome may contract_call the escrow (`dispute`) AND the UMA
    bond currency (to `approve` the arbiter adapter to pull the stake). It still CANNOT move the
    escrowed job funds — only the bond currency, only to approve, only the dispute path.
    """
    escrow = escrow or config.ESCROW_V4_ADDRESS
    bond_currency = bond_currency or config.UMA_BOND_CURRENCY
    return {
        "policies": [{
            "name": "disputer-escrow-and-bond-allowlist",
            "type": "contract_call",
            "rules": {
                "effect": "allow",
                "when": {"chain_in": [CHAIN], "target_in": [
                    {"chain_id": CHAIN, "contract_addr": escrow},
                    {"chain_id": CHAIN, "contract_addr": bond_currency},
                ]},
                "deny_if": {"usage_limits": {"rolling_24h": {"tx_count_gt": tx_cap}}},
            },
        }],
        "completion_conditions": _completion(),
    }


def client_budget_transfer_pact(cap: str = "0.001") -> dict:
    """Budget cap: Client may transfer native gas up to `cap`; anything larger is DENIED."""
    return {
        "policies": [{
            "name": "client-budget-cap",
            "type": "transfer",
            "rules": {
                "effect": "allow",
                "when": {"chain_in": [CHAIN],
                         "token_in": [{"chain_id": CHAIN, "token_id": config.NATIVE_TOKEN_ID}]},
                "deny_if": {"amount_gt": cap},
            },
        }],
        "completion_conditions": _completion(),
    }


def review_pact(review_amount_gt: str = "0.0005") -> dict:
    """Soft-review: native transfers above the threshold require owner approval (review_if)."""
    return {
        "policies": [{
            "name": "client-review-threshold",
            "type": "transfer",
            "rules": {
                "effect": "allow",
                "when": {"chain_in": [CHAIN],
                         "token_in": [{"chain_id": CHAIN, "token_id": config.NATIVE_TOKEN_ID}]},
                "review_if": {"amount_gt": review_amount_gt},
            },
        }],
        "completion_conditions": _completion(),
    }


def dump_all() -> Path:
    """Write the rendered pact JSONs to docs/pacts/ for the risk-boundary deliverable."""
    out = REPO_ROOT / "docs" / "pacts"
    out.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "client_escrow_pact.json": client_escrow_pact(),
        "provider_pact.json": provider_pact(),
        "client_budget_transfer_pact.json": client_budget_transfer_pact(),
        "review_pact.json": review_pact(),
        # Phase 6.5 marketplace templates - bound to the v2 open-marketplace escrow (legacy).
        "client_escrow_pact_v2.json": client_escrow_pact(escrow=config.ESCROW_V2_ADDRESS),
        "provider_pact_v2.json": provider_pact(escrow=config.ESCROW_V2_ADDRESS),
        # v3 marketplace templates - bound to the commit-reveal escrow.
        "client_escrow_pact_v3.json": client_escrow_pact(escrow=config.ESCROW_V3_ADDRESS),
        "provider_pact_v3.json": provider_pact(escrow=config.ESCROW_V3_ADDRESS),
        # v4 marketplace templates - committee consensus + staked disputes (the live marketplace).
        "client_escrow_pact_v4.json": client_escrow_pact(escrow=config.ESCROW_V4_ADDRESS),
        "provider_pact_v4.json": provider_pact(escrow=config.ESCROW_V4_ADDRESS),
        "evaluator_pact_v4.json": evaluator_pact(escrow=config.ESCROW_V4_ADDRESS),
        "disputer_pact_v4.json": disputer_pact(escrow=config.ESCROW_V4_ADDRESS),
    }
    for name, spec in artifacts.items():
        (out / name).write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return out
