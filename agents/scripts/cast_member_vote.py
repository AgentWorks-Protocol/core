"""Cast one committee member's on-chain vote for a job via its own CAW wallet + evaluator_pact.

Used to complete a quorum when a member's in-run vote stalled (e.g. a clogged wallet or a rate-limited
model). The member still signs from its OWN CAW wallet under its OWN castVote-only Pact — the trustless
authority boundary is unchanged; this just re-drives the on-chain cast outside the autonomous loop.

Usage: python scripts/cast_member_vote.py <MemberName> <job_id> <approve:1|0>
"""
from __future__ import annotations

import asyncio, sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config, registry, pacts
import escrow_v4 as esc
from caw.client import CawWallet


async def main() -> None:
    member_name = sys.argv[1] if len(sys.argv) > 1 else "Evaluator B"
    job_id = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    approve = (sys.argv[3] != "0") if len(sys.argv) > 3 else True

    m = next(x for x in registry.evaluators() if x.name == member_name)
    print(f"casting {member_name} ({m.address}, wallet {m.wallet_id[:8]}) vote={approve} on job #{job_id}")
    async with CawWallet(api_url=config.CAW_API_URL, api_key=m.api_key,
                         wallet_uuid=m.wallet_id, name=member_name) as ew:
        # clean slate + fresh castVote-only pact (USDC-excluded)
        try:
            page = await ew.list_pacts(status="active")
            items = page if isinstance(page, list) else (page.get("items", []) if isinstance(page, dict) else [])
            for p in items:
                if isinstance(p, dict) and p.get("id"):
                    await ew.revoke_pact(p["id"])
        except Exception as e:
            print("revoke skip:", e)
        sub = await ew.submit_pact(intent=f"{member_name} votes on marketplace deliverables",
                                   spec=pacts.evaluator_pact(escrow=config.ESCROW_V4_ADDRESS),
                                   name=f"vote-{uuid4().hex[:6]}")
        pact = await ew.wait_pact_active(sub.get("pact_id"))
        async with ew.scoped(pact) as s:
            rid = f"vote-{uuid4().hex[:8]}"
            await s.contract_call(src_addr=m.address, contract_addr=config.ESCROW_V4_ADDRESS,
                                  calldata=esc.cast_vote(job_id, approve), chain_id=config.CHAIN_ID,
                                  request_id=rid, description=f"castVote[{member_name}]")
            rec = await s.wait_tx_final(rid, timeout=600.0)
            print("vote tx:", (rec or {}).get("transaction_hash"), "status:", (rec or {}).get("status_display"))


if __name__ == "__main__":
    asyncio.run(main())
