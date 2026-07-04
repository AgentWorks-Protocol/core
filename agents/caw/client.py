"""The ONE isolated CAW integration layer (CLAUDE.md §7.3).

Everything that touches the Cobo Agentic Wallet SDK lives here; the agents call this
wrapper, never `WalletAPIClient` directly. A CAW SDK change touches only this file.

All method signatures below are the VERIFIED v0.1.40 surface (see docs/FACTS.md):
  - create_wallet_address(wallet_uuid, *, chain_id=...)
  - list_balances(wallet_uuid=..., chain_id=..., token_id=...)
  - submit_pact(wallet_id=..., intent=..., spec=..., name=...)
  - get_pact(pact_id)
  - transfer_tokens(wallet_uuid, *, dst_addr, amount, token_id, chain_id, request_id, ...)
  - get_user_transaction_by_request_id(wallet_uuid, request_id)
  - list_audit_logs(wallet_id=..., ...)

Authority model (verified): submit a pact with the wallet key -> poll get_pact until
status == "active" -> the active pact carries its own scoped api_key -> make constrained
calls (transfer/contract_call) through a client built with THAT key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from cobo_agentic_wallet.client import WalletAPIClient

log = logging.getLogger("caw")

# Secret-bearing field names that must NEVER be logged (CLAUDE.md §4). The pact-scoped api_key an
# ACTIVE pact carries (see scoped()) is the critical one: get_pact / wait_pact_active return it on
# every poll, so an un-redacted _call log would write a live, constrained signing credential to the
# log sink. Normalized to lower-case with '-' -> '_' before matching.
_SECRET_KEYS = {
    "api_key", "apikey", "mnemonic", "private_key", "privatekey", "invitation_code",
    "invitationcode", "secret", "seed", "key_share", "keyshare", "password", "passwd",
}
_REDACTED = "***REDACTED***"
# Belt-and-suspenders: scrub secrets that survive as `"api_key": "..."` inside a stringified object
# (e.g. when the SDK returns a non-dict that json.dumps renders via default=str).
_SECRET_STR_RE = re.compile(
    r"(?i)(\"?(?:api[_-]?key|mnemonic|private[_-]?key|invitation[_-]?code|secret|seed|"
    r"key[_-]?share|passwd|password)\"?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,}\]]+)"
)


def _redact(obj: Any) -> Any:
    """Deep copy of `obj` with any secret-bearing keys masked, so credentials never reach logs/errors."""
    if isinstance(obj, dict):
        return {
            k: (_REDACTED if isinstance(k, str) and k.lower().replace("-", "_") in _SECRET_KEYS
                else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    return obj

# Terminal (non-active) pact statuses.
_PACT_TERMINAL = {"rejected", "expired", "revoked", "completed"}
# Transaction statuses we treat as final. CAW returns a numeric `status` (900 == Success)
# plus a human `status_display` and `sub_status`. We match on all three.
_TX_SUCCESS = {"success", "completed", "confirmed"}
_TX_FAILED = {"failed", "rejected", "declined", "dropped"}
_TX_STATUS_NUM_SUCCESS = {900}
_TX_STATUS_NUM_FAILED = {800, 1000}  # best-effort; status_display/sub_status are authoritative


def _short(obj: Any, n: int = 800) -> str:
    try:
        s = json.dumps(_redact(obj), default=str, ensure_ascii=False)
    except Exception:
        s = str(obj)
    s = _SECRET_STR_RE.sub(r"\1" + _REDACTED, s)
    return s if len(s) <= n else s[:n] + "…"


class CawWallet:
    """Async wrapper around a single CAW wallet (one api_key + wallet_uuid).

    Use as an async context manager. Logs every call + response so the audit trail is
    legible in the demo.
    """

    def __init__(self, *, api_url: str, api_key: str, wallet_uuid: str, name: str = "") -> None:
        self.api_url = api_url
        self.api_key = api_key
        self.wallet_uuid = wallet_uuid
        self.name = name or wallet_uuid[:8]
        self._client = WalletAPIClient(base_url=api_url, api_key=api_key)

    async def __aenter__(self) -> "CawWallet":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.close()

    async def _call(self, label: str, coro: Any) -> Any:
        log.info("[%s] -> %s", self.name, label)
        result = await coro
        log.info("[%s] <- %s: %s", self.name, label, _short(result))
        return result

    # ── wallet / address / balance ──

    async def create_address(self, chain_id: str) -> Any:
        return await self._call(
            f"create_wallet_address(chain_id={chain_id})",
            self._client.create_wallet_address(self.wallet_uuid, chain_id=chain_id),
        )

    async def list_addresses(self) -> Any:
        return await self._call(
            "list_wallet_addresses", self._client.list_wallet_addresses(self.wallet_uuid)
        )

    async def list_balances(self, *, chain_id: str | None = None, token_id: str | None = None) -> Any:
        return await self._call(
            f"list_balances(chain_id={chain_id}, token_id={token_id})",
            self._client.list_balances(self.wallet_uuid, chain_id=chain_id, token_id=token_id),
        )

    # ── pacts ──

    async def submit_pact(self, *, intent: str, spec: dict, name: str | None = None) -> Any:
        return await self._call(
            f"submit_pact(name={name})",
            self._client.submit_pact(
                wallet_id=self.wallet_uuid, intent=intent, spec=spec, name=name
            ),
        )

    async def get_pact(self, pact_id: str) -> Any:
        return await self._call(f"get_pact({pact_id})", self._client.get_pact(pact_id))

    async def wait_pact_active(self, pact_id: str, *, timeout: float = 120.0, interval: float = 3.0) -> dict:
        """Poll until the pact is active. Unpaired wallets auto-activate."""
        started = time.monotonic()
        last = None
        while True:
            pact = await self.get_pact(pact_id)
            status = (pact or {}).get("status", "")
            if status != last:
                log.info("[%s] pact %s status -> %s", self.name, pact_id, status)
                last = status
            if status == "active":
                return pact
            if status in _PACT_TERMINAL:
                raise RuntimeError(f"pact {pact_id} reached terminal status before use: {status}")
            if time.monotonic() - started > timeout:
                raise TimeoutError(f"pact {pact_id} not active after {timeout}s (last status={status})")
            await asyncio.sleep(interval)

    def scoped(self, pact: dict, *, name_suffix: str = "+pact") -> "CawWallet":
        """Return a CawWallet bound to the pact-scoped api_key if the pact provides one,
        otherwise reuse this wallet's key (same wallet_uuid either way)."""
        key = (pact or {}).get("api_key") or self.api_key
        return CawWallet(
            api_url=self.api_url, api_key=key, wallet_uuid=self.wallet_uuid,
            name=f"{self.name}{name_suffix}",
        )

    # ── transfers ──

    async def transfer(
        self, *, src_addr: str, dst_addr: str, amount: str, token_id: str, chain_id: str,
        request_id: str, description: str | None = None,
    ) -> Any:
        return await self._call(
            f"transfer_tokens({amount} {token_id} {src_addr} -> {dst_addr})",
            self._client.transfer_tokens(
                self.wallet_uuid, src_addr=src_addr, dst_addr=dst_addr, amount=amount,
                token_id=token_id, chain_id=chain_id, request_id=request_id, description=description,
            ),
        )

    async def contract_call(
        self, *, src_addr: str, contract_addr: str, calldata: str, chain_id: str,
        request_id: str, value: str = "0", description: str | None = None,
        private_tx: bool = False,
    ) -> Any:
        # Private-mempool routing hook (defense-in-depth for the commit-reveal accept; see docs/MEV.md).
        # CAW signs + broadcasts every tx via its TSS relay; the verified v0.1.40 SDK surface exposes
        # NO private-order-flow / Flashbots field. We therefore log the intent here - the single,
        # isolated chokepoint - so the day Cobo adds a `private`/`mev_protect` kwarg, this is the only
        # line that changes. We do NOT bypass CAW with local signing (that would break "cloud holds no
        # keys / one TSS node"). Honest stance: this is prepared, not yet active end-to-end.
        if private_tx:
            log.info("[%s] private-mempool routing requested for %s (relay support pending)",
                     self.name, request_id)
        return await self._call(
            f"contract_call({contract_addr} data={calldata[:18]}… private={private_tx})",
            self._client.contract_call(
                self.wallet_uuid, src_addr=src_addr, chain_id=chain_id, contract_addr=contract_addr,
                calldata=calldata, value=value, request_id=request_id, description=description,
            ),
        )

    async def get_tx_by_request_id(self, request_id: str) -> Any:
        return await self._call(
            f"get_user_transaction_by_request_id({request_id})",
            self._client.get_user_transaction_by_request_id(self.wallet_uuid, request_id),
        )

    async def wait_tx_final(self, request_id: str, *, timeout: float = 180.0, interval: float = 5.0) -> dict:
        """Poll the transaction record until it reaches a final status. Returns the record."""
        started = time.monotonic()
        last = None
        while True:
            try:
                rec = await self.get_tx_by_request_id(request_id)
            except Exception as e:  # record may not exist yet immediately after submit
                rec = None
                log.info("[%s] tx record not ready yet (%s)", self.name, e)
            rec = rec or {}
            disp = str(rec.get("status_display", "")).lower()
            sub = str(rec.get("sub_status", "")).lower()
            num = rec.get("status")
            tag = disp or sub or str(num)
            if tag and tag != last:
                log.info("[%s] tx %s status -> %s (num=%s)", self.name, request_id, tag, num)
                last = tag
            if disp in _TX_SUCCESS or sub in {"completed", "confirmed"} or num in _TX_STATUS_NUM_SUCCESS:
                return rec
            if disp in _TX_FAILED or sub in {"failed", "rejected"} or num in _TX_STATUS_NUM_FAILED:
                raise RuntimeError(f"transfer {request_id} failed: rec={_short(rec)}")
            if time.monotonic() - started > timeout:
                raise TimeoutError(f"transfer {request_id} not final after {timeout}s (last={last}, rec={_short(rec)})")
            await asyncio.sleep(interval)

    # ── audit ──

    async def list_audit_logs(self, *, limit: int = 50, action: str | None = None) -> Any:
        return await self._call(
            f"list_audit_logs(limit={limit}, action={action})",
            self._client.list_audit_logs(wallet_id=self.wallet_uuid, action=action, limit=limit),
        )

    # ── pact management (clean slate + emergency freeze) ──

    async def list_pacts(self, *, status: Any | None = None) -> Any:
        return await self._call(
            f"list_pacts(status={status})",
            self._client.list_pacts(status=status, wallet_id=self.wallet_uuid),
        )

    async def revoke_pact(self, pact_id: str) -> Any:
        return await self._call(f"revoke_pact({pact_id})", self._client.revoke_pact(pact_id))

    # ── pending operations (review_if / always_review approvals) ──

    async def list_pending_operations(self, *, status: Any | None = None) -> Any:
        return await self._call(
            f"list_pending_operations(status={status})",
            self._client.list_pending_operations(status=status),
        )

    async def get_pending_operation(self, op_id: str) -> Any:
        return await self._call(
            f"get_pending_operation({op_id})", self._client.get_pending_operation(op_id)
        )

    async def approve_pending_operation(self, op_id: str) -> Any:
        return await self._call(
            f"approve_pending_operation({op_id})", self._client.approve_pending_operation(op_id)
        )

    async def reject_pending_operation(self, op_id: str, *, reason: str | None = None) -> Any:
        return await self._call(
            f"reject_pending_operation({op_id})",
            self._client.reject_pending_operation(op_id, reason=reason),
        )
