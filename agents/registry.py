"""Marketplace participant registry (Phase 6.5).

A POOL of CAW wallets that take roles in the open marketplace. v1 used two hardcoded wallets
(Client, Provider); this generalizes to N. Each participant binds a TEMPLATE Pact (the
permission/isolation boundary) when it acts.

Sources, in order:
  1. the canonical Client (CAW_CLIENT_*) and Provider (CAW_PROVIDER_*) from config,
  2. auto-discovered extra providers CAW_PROVIDER2_*, CAW_PROVIDER3_*, … (WALLET_ID/API_KEY/ADDRESS),
  3. optional external participants from `agents/registry.local.json` (the documented self-service
     path - users bring their own CAW wallet; that file holds keys so it is gitignored, never committed).

This invents NO new SDK surface: a participant is just an (api_key, wallet_uuid) pair driven through
the existing CawWallet, and onboarding = submit_pact(template) + wait_pact_active (agents/caw/client.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import config
import pacts
from caw import CawWallet

def _data_dir() -> Path:
    return Path(os.environ["AGENT_DATA_DIR"]) if os.environ.get("AGENT_DATA_DIR") \
        else Path(__file__).resolve().parent


# External participants' CUSTODIAL credentials (holds api_keys). On a host with ephemeral storage set
# AGENT_DATA_DIR to a mounted volume so registrations persist across restarts. Always gitignored - never committed.
_LOCAL_FILE = _data_dir() / "registry.local.json"

# The KEYLESS discovery directory (Phase: production registration). Holds NO api_keys - only non-secret
# identity + self-reported pact status - so it is COMMIT-SAFE. This is DISCOVERY, not security: enforcement
# stays with CAW (the Pact) + the escrow (only the contract moves funds). A lying entry can't sign, so it
# can't do harm. Entries here are SELF-DRIVEN (no key held → the platform never auto-drives them).
_DIRECTORY_FILE = _data_dir() / "registry.directory.json"


@dataclass(frozen=True)
class Participant:
    name: str
    role: str        # 'client' | 'provider' | 'evaluator'  (committee member)
    wallet_id: str
    api_key: str
    address: str
    tx_cap: int = 0  # 0 → use the template's default cap
    # Per-member LLM (committee independence axis 2): each evaluator can reason on a DISTINCT model so a
    # quorum is genuine — not one model voting N times. Blank → fall back to the global default (config.LLM_*).
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    # Ownership + provenance (non-secret). owner_mode: 'unpaired' (agent-owned, Pact auto-activates) |
    # 'paired' (human-owned, in-app Cobo approval). source: 'env' | 'custodial' | 'self' (keyless directory).
    owner_mode: str = "unpaired"
    source: str = "env"

    @property
    def is_self_driven(self) -> bool:
        """A participant with NO api_key is a keyless directory entry: it self-drives (via MCP / the
        calldata rail) and the platform can NEVER auto-drive it. `bool(api_key)` is the SINGLE predicate
        that decides auto-drive vs self-drive everywhere in the orchestrator."""
        return not self.api_key

    def llm(self) -> dict:
        """Resolved LLM config for this participant, falling back to the global default (config.LLM_*)."""
        return {
            "api_key": self.llm_api_key or config.LLM_API_KEY,
            "base_url": self.llm_base_url or config.LLM_BASE_URL,
            "model": self.llm_model or config.LLM_MODEL,
        }

    def template(self) -> dict:
        """The scoped Pact spec this participant binds, pinned to the live v4 escrow."""
        if self.role == "provider":
            return pacts.provider_pact(escrow=config.ESCROW_V4_ADDRESS, tx_cap=self.tx_cap or 20)
        if self.role == "evaluator":
            return pacts.evaluator_pact(escrow=config.ESCROW_V4_ADDRESS, tx_cap=self.tx_cap or 20)
        return pacts.client_escrow_pact(escrow=config.ESCROW_V4_ADDRESS, tx_cap=self.tx_cap or 50)

    def public(self) -> dict:
        """Non-secret view (never includes api_key) - safe to log / return over HTTP."""
        return {"name": self.name, "role": self.role, "wallet_id": self.wallet_id,
                "address": self.address, "owner_mode": self.owner_mode, "source": self.source,
                "driven": "self" if self.is_self_driven else "auto",
                "llm_model": self.llm_model}


def _from_env(prefix: str, *, name: str, role: str) -> Participant | None:
    wid = os.environ.get(f"CAW_{prefix}_WALLET_ID")
    key = os.environ.get(f"CAW_{prefix}_API_KEY")
    addr = os.environ.get(f"CAW_{prefix}_ADDRESS")
    if not (wid and key and addr):
        return None
    return Participant(name=name, role=role, wallet_id=wid, api_key=key, address=addr)


def _from_local_file() -> list[Participant]:
    if not _LOCAL_FILE.exists():
        return []
    try:
        rows = json.loads(_LOCAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[Participant] = []
    for r in rows if isinstance(rows, list) else []:
        if r.get("wallet_id") and r.get("api_key") and r.get("address"):
            out.append(Participant(
                name=r.get("name", r["address"][:10]), role=r.get("role", "provider"),
                wallet_id=r["wallet_id"], api_key=r["api_key"], address=r["address"],
                tx_cap=int(r.get("tx_cap", 0)),
                owner_mode=r.get("owner_mode", "unpaired"), source="custodial",
            ))
    return out


def _read_directory() -> list[dict]:
    """Raw keyless directory rows (as stored). Never contains api_keys."""
    if not _DIRECTORY_FILE.exists():
        return []
    try:
        rows = json.loads(_DIRECTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def _from_directory_file() -> list[Participant]:
    """Keyless directory entries as SELF-DRIVEN Participants (api_key='' → never auto-driven)."""
    out: list[Participant] = []
    for r in _read_directory():
        if r.get("wallet_id") and r.get("address"):
            out.append(Participant(
                name=r.get("name") or r["address"][:10], role=r.get("role", "provider"),
                wallet_id=r["wallet_id"], api_key="", address=r["address"],
                llm_model=r.get("llm_model", ""),
                owner_mode=r.get("owner_mode", "unpaired"), source="self",
            ))
    return out


def directory() -> list[dict]:
    """The ONE canonical public participant list for GET /marketplace/directory — env platform-operated +
    custodial + keyless self-registered, every role. No secrets (public() never includes api_key). Keyless
    rows are enriched with their self-reported pact status. This is the single source of truth the dashboard
    (DirectoryPanel + the PostJob committee picker) reads, so no surface can under-report participants."""
    keyless = {str(r.get("address", "")).lower(): r for r in _read_directory()}
    out: list[dict] = []
    for p in load_pool():
        row = p.public()
        k = keyless.get(p.address.lower())
        if k:
            row["pact_status"] = k.get("pact_status")
            row["pact_id"] = k.get("pact_id")
            row["registered_at"] = k.get("registered_at")
        out.append(row)
    return out


def load_pool() -> list[Participant]:
    """The marketplace participant pool. Always includes the canonical Client + Provider; adds any
    CAW_PROVIDER{N}_* found in env, plus external participants from registry.local.json."""
    pool: list[Participant] = []
    client = _from_env("CLIENT", name="Client", role="client")
    if client:
        pool.append(client)
    provider = _from_env("PROVIDER", name="Provider", role="provider")
    if provider:
        pool.append(provider)
        # Extra ADDRESSES on the SAME provider wallet (CAW_PROVIDER_ADDRESS_2, _3, …). A second
        # address is a distinct on-chain msg.sender signed by the same Provider TSS node and bound
        # by the same provider Pact - a genuine second provider for the accept-race without a whole
        # new wallet/daemon. (Public address only; reuses the provider's wallet_id + api_key.)
        n = 2
        while True:
            extra = os.environ.get(f"CAW_PROVIDER_ADDRESS_{n}")
            if not extra:
                break
            pool.append(Participant(name=f"Provider{chr(64 + n)}", role="provider",  # ProviderB, ProviderC, …
                                    wallet_id=provider.wallet_id, api_key=provider.api_key,
                                    address=extra, tx_cap=provider.tx_cap))
            n += 1
    # auto-discover fully separate provider wallets CAW_PROVIDER2_*, CAW_PROVIDER3_*, …
    n = 2
    while True:
        p = _from_env(f"PROVIDER{n}", name=f"Provider{n}", role="provider")
        if p is None:
            break
        pool.append(p)
        n += 1
    pool.extend(_from_local_file())
    # Keyless directory entries (self-driven). Dedupe by address: an env/custodial entry (holds a usable
    # api_key → auto-drivable) wins over a keyless directory row for the same address.
    seen = {p.address.lower() for p in pool}
    for p in _from_directory_file():
        if p.address.lower() not in seen:
            pool.append(p)
            seen.add(p.address.lower())
    # The env-configured evaluator COMMITTEE (CAW_EVALUATOR_*) is produced ONLY by evaluators(); merge it so
    # /health, /marketplace/participants, and the directory all report the committee. (Custodial/keyless
    # evaluators are already in the pool via the merges above; the address dedupe drops the overlap.)
    for p in evaluators():
        if p.address.lower() not in seen:
            pool.append(p)
            seen.add(p.address.lower())
    return pool


def providers(pool: list[Participant] | None = None) -> list[Participant]:
    return [p for p in (pool or load_pool()) if p.role == "provider"]


def trusted_evaluator_addrs() -> set[str]:
    """Lowercased addresses of PLATFORM-OPERATED evaluators (the env-configured committee) — the set a
    provider trusts to judge its work fairly before it claims a job. Deliberately EXCLUDES self-registered
    directory evaluators (source != 'env'): otherwise an attacker could self-register sock-puppet
    'evaluators', name them as a job's committee, and rig it to reject honest work → refund itself."""
    return {p.address.lower() for p in evaluators() if p.source == "env"}


def evaluators() -> list[Participant]:
    """The evaluator COMMITTEE.

    Preferred (Phase 8.2b): each member is a SEPARATE CAW wallet AND a SEPARATE LLM —
    `CAW_EVALUATOR_{n}_WALLET_ID/_API_KEY/_ADDRESS` (+ optional `_LLM_API_KEY/_LLM_BASE_URL/_LLM_MODEL`).
    Independent on BOTH axes (signing key + reasoning model), so a quorum is a genuine M-of-N of
    independent judges — not one wallet/LLM voting N times. (A blank per-member LLM falls back to the
    global default `config.LLM_*`.)

    Legacy fallback: one shared wallet with extra addresses (`CAW_EVALUATOR_WALLET_ID/_API_KEY` +
    `CAW_EVALUATOR_ADDRESS_1.._N`), all on the default LLM — the original demo committee.

    Returns [] if neither is configured. Production independence ultimately comes from external operators
    each running their own evaluator wallet + model (`MCP_ROLE=evaluator`; docs/ARBITRATION.md §5)."""
    out: list[Participant] = []
    n = 1
    while True:  # preferred: separate wallet + model per member
        wid = os.environ.get(f"CAW_EVALUATOR_{n}_WALLET_ID")
        key = os.environ.get(f"CAW_EVALUATOR_{n}_API_KEY")
        addr = os.environ.get(f"CAW_EVALUATOR_{n}_ADDRESS")
        if not (wid and key and addr):
            break
        out.append(Participant(
            name=f"Evaluator {chr(64 + n)}", role="evaluator", wallet_id=wid, api_key=key, address=addr,
            llm_api_key=os.environ.get(f"CAW_EVALUATOR_{n}_LLM_API_KEY", ""),
            llm_base_url=os.environ.get(f"CAW_EVALUATOR_{n}_LLM_BASE_URL", ""),
            llm_model=os.environ.get(f"CAW_EVALUATOR_{n}_LLM_MODEL", ""),
        ))
        n += 1
    if not out:
        # legacy fallback: one shared wallet, extra addresses, shared default LLM
        wid = os.environ.get("CAW_EVALUATOR_WALLET_ID")
        key = os.environ.get("CAW_EVALUATOR_API_KEY")
        if wid and key:
            n = 1
            while True:
                addr = os.environ.get(f"CAW_EVALUATOR_ADDRESS_{n}")
                if not addr:
                    break
                out.append(Participant(name=f"Evaluator {chr(64 + n)}", role="evaluator",  # Evaluator A, B, C…
                                       wallet_id=wid, api_key=key, address=addr))
                n += 1
    # merge CUSTODIAL (registry.local.json) + KEYLESS (registry.directory.json) evaluators, deduped by
    # address against the env committee. (Custodial evaluators are auto-drivable; keyless self-vote.)
    seen = {p.address.lower() for p in out}
    for p in _from_local_file() + _from_directory_file():
        if p.role == "evaluator" and p.address.lower() not in seen:
            out.append(p)
            seen.add(p.address.lower())
    return out


def client(pool: list[Participant] | None = None) -> Participant:
    for p in (pool or load_pool()):
        if p.role == "client":
            return p
    raise RuntimeError("no client participant in the registry (set CAW_CLIENT_* in .env)")


# ── onboarding (bind the role's template Pact) ──────────────────────────────

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


async def onboard(p: Participant, *, clean: bool = True, timeout: float = 120.0) -> dict:
    """Submit the participant's template Pact and wait until active. Returns a NON-SECRET summary
    (the scoped api_key is never returned/logged)."""
    async with CawWallet(api_url=config.CAW_API_URL, api_key=p.api_key,
                         wallet_uuid=p.wallet_id, name=p.name) as w:
        if clean:
            await _revoke_active(w)
        sub = await w.submit_pact(
            intent=f"{p.role} in the AgentWorks open marketplace",
            spec=p.template(), name=f"mkt-{p.role}-{p.wallet_id[:8]}",
        )
        pact = await w.wait_pact_active(sub.get("pact_id"), timeout=timeout)
        return {
            **p.public(),
            "pact_id": pact.get("id") or sub.get("pact_id"),
            "pact_status": pact.get("status"),
            "has_scoped_key": bool(pact.get("api_key")),
        }


_ROLES = ("client", "provider", "evaluator")


def register_directory(*, address: str, wallet_id: str, role: str = "provider",
                       name: str | None = None, pact_id: str | None = None,
                       owner_mode: str = "unpaired", llm_model: str = "",
                       source: str = "self") -> dict:
    """NON-CUSTODIAL registration: record a KEYLESS discovery entry. Takes NO api_key and binds NO Pact
    (the platform can't sign for an external agent). The operator self-binds the role's scoped Pact via MCP
    or the Cobo app, then lists here so others can discover them. Upsert by address. Returns the public row.

    The directory is DISCOVERY, not security: `pact_status` is self-reported (advisory); enforcement stays
    with CAW (the Pact) + the escrow. A keyless entry that lies can't sign, so it can't move funds.
    """
    if role not in _ROLES:
        raise ValueError(f"role must be one of {_ROLES}, got '{role}'")
    if owner_mode not in ("unpaired", "paired"):
        raise ValueError(f"owner_mode must be 'unpaired' or 'paired', got '{owner_mode}'")
    if not (address and wallet_id):
        raise ValueError("address and wallet_id are required")

    row = {
        "name": name or f"ext-{address[:10]}", "role": role, "address": address,
        "wallet_id": wallet_id, "pact_id": pact_id,
        "pact_status": "self-reported" if pact_id else "unverified",
        "owner_mode": owner_mode, "llm_model": llm_model, "source": source,
        "registered_at": int(time.time()), "last_seen": int(time.time()),
    }
    rows = _read_directory()
    # upsert by address (case-insensitive)
    rows = [r for r in rows if str(r.get("address", "")).lower() != address.lower()]
    rows.append(row)
    _DIRECTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DIRECTORY_FILE.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return row


async def register_external(wallet_id: str, api_key: str, address: str,
                            role: str = "provider", name: str | None = None,
                            tx_cap: int = 0) -> dict:
    """CUSTODIAL registration (the labeled quick-path). Creates a scoped Pact via CAW, persists the
    participant — INCLUDING its api_key — to registry.local.json, and returns a non-secret summary.

    External agents bring their own CAW wallet (wallet_id + api_key). The platform holds that api_key and
    can auto-drive the wallet; that is why this path is custodial. The trustless path is register_directory
    (keyless) + the operator self-driving via MCP / the calldata rail.
    """
    if role not in _ROLES:
        raise ValueError(f"role must be one of {_ROLES}, got '{role}'")

    # Check if already registered (by wallet_id)
    existing = _from_local_file()
    for p in existing:
        if p.wallet_id == wallet_id:
            # Already registered - just onboard (refresh Pact)
            result = await onboard(p, clean=True)
            return {**result, "already_registered": True}

    p = Participant(
        name=name or f"ext-{address[:10]}",
        role=role,
        wallet_id=wallet_id,
        api_key=api_key,
        address=address,
        tx_cap=tx_cap,
    )

    # Create the Pact
    result = await onboard(p, clean=True)

    # Persist to registry.local.json (gitignored - holds credentials)
    existing.append(p)
    _LOCAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LOCAL_FILE.write_text(
        json.dumps([{
            "name": pr.name, "role": pr.role, "wallet_id": pr.wallet_id,
            "api_key": pr.api_key, "address": pr.address, "tx_cap": pr.tx_cap,
        } for pr in existing], indent=2),
        encoding="utf-8",
    )

    return {**result, "already_registered": False}


# ── CLI: print the pool (no secrets); `--onboard` binds each template live ──

def _main() -> None:
    import sys

    pool = load_pool()
    print(json.dumps({
        "escrow_v2": config.ESCROW_V2_ADDRESS,
        "count": len(pool),
        "providers": len(providers(pool)),
        "participants": [p.public() for p in pool],
    }, indent=2))

    if "--onboard" in sys.argv:
        async def run():
            for p in pool:
                try:
                    res = await onboard(p)
                    print("ONBOARDED:", json.dumps(res))
                except Exception as e:  # surface, don't hide
                    print(f"FAILED {p.name}: {type(e).__name__}: {e}")
        asyncio.run(run())


if __name__ == "__main__":
    _main()
