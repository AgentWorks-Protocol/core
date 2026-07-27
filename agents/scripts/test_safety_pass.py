"""Unit tests for the Week-4 real-money safety pass. No live LLM / CAW / chain needed.

Covers the three enforceable protections added for the Base mainnet deploy:
  1. The per-job USDC reward CEILING (config.MAX_JOB_REWARD_USDC) actually rejects over-cap / non-positive
     posts in server.marketplace_post_calldata (the app-layer value cap; CAW does not bound contract-call
     USDC value — see agents/pacts.py client_escrow_pact docstring).
  2. The CAW pact boundaries: client allowlist = escrow + USDC with a tx-count cap; provider EXCLUDES USDC.
  3. The spend-path ARM GATE: with AGENT_ARM_TOKEN set, the service boots DISARMED (no standing agents),
     POST /arm is bearer-gated, and /disarm stops them again.

Run:
    agents/.venv/Scripts/python.exe agents/scripts/test_safety_pass.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Arm token MUST be set before importing server (read at module import) so the disarmed-boot path is exercised.
os.environ["AGENT_ARM_TOKEN"] = "test-arm-token"
os.environ.setdefault("PLATFORM_AGENTS", "1")
os.environ.setdefault("SETTLEMENT_WATCHER", "1")

import config          # noqa: E402
import pacts           # noqa: E402
import autonomous      # noqa: E402
import server          # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_passed = 0


def check(label, cond):
    global _passed
    assert cond, f"FAIL: {label}"
    _passed += 1
    print(f"  ok: {label}")


def test_reward_ceiling():
    committee = "0x1111111111111111111111111111111111111111,0x2222222222222222222222222222222222222222,0x3333333333333333333333333333333333333333"
    over = config.MAX_JOB_REWARD_USDC + 1

    # Over the ceiling -> 400, and it must NOT reach the chain read (the guard is before esc.web3()).
    try:
        server.marketplace_post_calldata("0xabc", over, task="t", criteria="c", committee=committee)
        check("over-cap amount rejected", False)
    except HTTPException as e:
        check("over-cap amount -> HTTP 400", e.status_code == 400 and "ceiling" in e.detail)

    # Non-positive -> 400.
    try:
        server.marketplace_post_calldata("0xabc", 0, task="t", criteria="c", committee=committee)
        check("zero amount rejected", False)
    except HTTPException as e:
        check("zero amount -> HTTP 400", e.status_code == 400)

    # Missing committee is still rejected first (unchanged behaviour), independent of amount.
    try:
        server.marketplace_post_calldata("0xabc", 1.0, task="t", criteria="c", committee="")
        check("missing committee rejected", False)
    except HTTPException as e:
        check("missing committee -> HTTP 400", e.status_code == 400)

    check("MAX_JOB_REWARD_USDC is a positive default", config.MAX_JOB_REWARD_USDC > 0)


def test_pact_boundaries():
    c = pacts.client_escrow_pact(escrow="0xESCROW", usdc="0xUSDC")
    rule = c["policies"][0]["rules"]
    targets = {t["contract_addr"] for t in rule["when"]["target_in"]}
    check("client pact allowlists escrow + USDC", targets == {"0xESCROW", "0xUSDC"})
    check("client pact carries a rolling-24h tx-count cap",
          "tx_count_gt" in rule["deny_if"]["usage_limits"]["rolling_24h"])
    check("client pact tx cap tightened to 24", rule["deny_if"]["usage_limits"]["rolling_24h"]["tx_count_gt"] == 24)

    p = pacts.provider_pact(escrow="0xESCROW")
    ptargets = {t["contract_addr"] for t in p["policies"][0]["rules"]["when"]["target_in"]}
    check("provider pact EXCLUDES USDC (escrow only)", ptargets == {"0xESCROW"})


def test_arm_gate():
    # Replace the real standing-agent coroutines with trivial ones so arming doesn't touch CAW/chain.
    async def _idle(stop):
        await stop.wait()

    autonomous.platform_agents = _idle
    autonomous.has_first_party_signer = lambda: False  # skip the watcher branch

    with TestClient(server.app) as client:
        h = client.get("/health").json()
        check("boots DISARMED when arm token set", h["armed"] is False and h["arm_protected"] is True)
        check("no standing services active at boot", h["watcher"]["active"] is False)

        r = client.post("/arm")  # no bearer
        check("POST /arm without token -> 401", r.status_code == 401)

        r = client.post("/arm", headers={"Authorization": "Bearer test-arm-token"})
        check("POST /arm with token -> 200 armed", r.status_code == 200 and r.json()["armed"] is True)
        check("standing service now active", client.get("/health").json()["watcher"]["active"] is True)

        r = client.post("/arm", headers={"Authorization": "Bearer test-arm-token"})
        check("re-arm is idempotent (already_active)", r.json().get("already_active") is True)

        r = client.post("/disarm", headers={"Authorization": "Bearer test-arm-token"})
        check("POST /disarm -> stopped", r.status_code == 200 and r.json()["armed"] is False)


def run():
    test_reward_ceiling()
    test_pact_boundaries()
    test_arm_gate()
    print(f"\nALL {_passed} CHECKS PASSED")


if __name__ == "__main__":
    run()
