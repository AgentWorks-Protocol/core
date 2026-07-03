"""Shared config for the AgentWorks agents. Single source of env truth.

Loads the repo-root .env. Holds CAW credentials, Ethereum Sepolia chain/token ids
(confirmed in Phase 2), and on-chain contract addresses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")


def _req(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name} (see repo-root .env)")
    return v


@dataclass(frozen=True)
class Agent:
    name: str
    wallet_id: str
    api_key: str
    address: str


# CAW API
CAW_API_URL = os.environ.get("AGENT_WALLET_API_URL", "https://api.agenticwallet.cobo.com")

# Ethereum Sepolia CAW ids (the project switched off Base Sepolia in Phase 2; .env overrides these).
CHAIN_ID = os.environ.get("CAW_CHAIN_ID", "SETH")
NATIVE_TOKEN_ID = os.environ.get("CAW_NATIVE_TOKEN_ID", "SETH")
USDC_TOKEN_ID = os.environ.get("CAW_USDC_TOKEN_ID", "SETH_USDC")

# On-chain (Ethereum Sepolia)
RPC_URL = os.environ.get("RPC_URL", "https://sepolia.drpc.org")
# Optional private/MEV-protected RPC. When set it is preferred for reads (harmless) and is the
# endpoint a private-broadcast path would use once the CAW relay exposes private order flow. See
# docs/MEV.md. Defensive default: unset → fall back to RPC_URL.
PRIVATE_RPC_URL = os.environ.get("PRIVATE_RPC_URL", "")
# Opt-in flag: when true, state-changing accept txs (the reveal) signal a request for private-mempool
# routing through the single CAW chokepoint (caw/client.py). A documented no-op until Cobo supports it.
MEV_PROTECT = os.environ.get("MEV_PROTECT", "false").strip().lower() in ("1", "true", "yes", "on")
ESCROW_ADDRESS = os.environ.get("ESCROW_CONTRACT_ADDRESS", "")  # v1 (closed 1:1) - legacy
# v2 open-marketplace escrow (Phase 6.5). Raw acceptJob race - superseded by v3 commit-reveal but
# kept for history; defaults to the deployed+verified address.
ESCROW_V2_ADDRESS = os.environ.get("ESCROW_V2_CONTRACT_ADDRESS", "0xD6cB413c0E4a5839Fd4B02aFFeBF65e6868726b9")
# v3 open-marketplace escrow with sealed commit-reveal accept (MEV/frontrunning hardening).
# Live + verified on Sepolia (deploy block 11087195). Override in .env to point at another deployment.
ESCROW_V3_ADDRESS = os.environ.get("ESCROW_V3_CONTRACT_ADDRESS", "0xFAab4d6ff5CBEcD72a4e1B9315662e7846166D69")
# Reveal timing must mirror the deployed v3 constructor args (delay=1, window=256 on Sepolia) so the
# agents wait the right number of blocks between commitAccept and revealAccept.
REVEAL_DELAY_BLOCKS = int(os.environ.get("REVEAL_DELAY_BLOCKS", "1"))
REVEAL_WINDOW_BLOCKS = int(os.environ.get("REVEAL_WINDOW_BLOCKS", "256"))
# v4 open-marketplace escrow: committee (M-of-N) evaluation + staked disputes escalating to a decoupled,
# decentralized arbiter (UMA Optimistic Oracle V3). Live + verified on Sepolia (deploy block 11189574).
ESCROW_V4_ADDRESS = os.environ.get("ESCROW_V4_CONTRACT_ADDRESS", "0x8F60e34e43Dd53Bd170633fB5b1d8c43e21C264C")
# The decoupled arbiter adapter (IS the escrow's `arbiter`; rules via UMA OOv3, never an operator key).
UMA_ARBITER_ADDRESS = os.environ.get("UMA_ARBITER_ADDRESS", "0x8BDB79EB6cDC3E54E373C0E5096CffD737a5DE4B")
# UMA Optimistic Oracle V3 on Sepolia + its whitelisted bond currency. We use UMA's zero-minimum-bond,
# publicly-mintable test token "6TEST" (6-dp) so a real dispute is demonstrable live on Sepolia; the
# escrow settlement token stays MockUSDC. Production sets a meaningful bond + (on mainnet) real USDC.
UMA_OOV3_ADDRESS = os.environ.get("UMA_OOV3_ADDRESS", "0xFd9e2642a170aDD10F53Ee14a93FcF2F31924944")
UMA_BOND_CURRENCY = os.environ.get("UMA_BOND_CURRENCY", "0x3870419Ba2BBf0127060bCB37f69A1b1C090992B")
UMA_BOND = int(os.environ.get("UMA_BOND", "5000000"))  # 5 6TEST (zero-min currency; demo stake)
# v4 settlement timing (must mirror the deployed v4 ctor args). Sepolia demo values are small.
VOTING_WINDOW_BLOCKS = int(os.environ.get("VOTING_WINDOW_BLOCKS", "50"))
DISPUTE_WINDOW_BLOCKS = int(os.environ.get("DISPUTE_WINDOW_BLOCKS", "30"))
DISPUTE_RESOLVE_WINDOW_BLOCKS = int(os.environ.get("DISPUTE_RESOLVE_WINDOW_BLOCKS", "50"))
# Evaluator committee defaults for the autonomous run (odd N; quorum = strict majority).
COMMITTEE_SIZE = int(os.environ.get("COMMITTEE_SIZE", "3"))
COMMITTEE_QUORUM = int(os.environ.get("COMMITTEE_QUORUM", "2"))
USDC_ADDRESS = os.environ.get("USDC_TOKEN_ADDRESS", "")
EXPLORER_TX = "https://sepolia.etherscan.io/tx/{}"
EXPLORER_ADDR = "https://sepolia.etherscan.io/address/{}"


# LLM (agent reasoning) - DeepSeek via OpenAI-compatible API
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")


def client_agent() -> Agent:
    return Agent(
        name="Client",
        wallet_id=_req("CAW_CLIENT_WALLET_ID"),
        api_key=_req("CAW_CLIENT_API_KEY"),
        address=_req("CAW_CLIENT_ADDRESS"),
    )


def provider_agent() -> Agent:
    return Agent(
        name="Provider",
        wallet_id=_req("CAW_PROVIDER_WALLET_ID"),
        api_key=_req("CAW_PROVIDER_API_KEY"),
        address=_req("CAW_PROVIDER_ADDRESS"),
    )


def evaluator_agent() -> Agent:
    """The evaluator-committee CAW wallet (hosts the committee member addresses CAW_EVALUATOR_ADDRESS_1..N).
    Address field is the wallet's primary address; committee member addresses come from registry.evaluators()."""
    return Agent(
        name="Evaluator",
        wallet_id=_req("CAW_EVALUATOR_WALLET_ID"),
        api_key=_req("CAW_EVALUATOR_API_KEY"),
        address=os.environ.get("CAW_EVALUATOR_ADDRESS_1", ""),
    )
