"""One-off: move native SETH gas from the Provider CAW wallet (which has excess) to the Client CAW
wallet (drained by the prior job + an 18-gwei gas spike), so the client can afford createJob/fund/finalize."""
from __future__ import annotations

import asyncio, sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import pacts
from caw import CawWallet

AMOUNT = "0.015"


async def main() -> None:
    pp = config.provider_agent()
    cc = config.client_agent()
    async with CawWallet(api_url=config.CAW_API_URL, api_key=pp.api_key,
                         wallet_uuid=pp.wallet_id, name="Provider") as pw:
        try:
            page = await pw.list_pacts(status="active")
            items = page if isinstance(page, list) else (page.get("items", []) if isinstance(page, dict) else [])
            for p in items:
                if isinstance(p, dict) and p.get("id"):
                    await pw.revoke_pact(p["id"])
        except Exception as e:
            print("revoke skip:", e)
        sub = await pw.submit_pact(intent="Top up client gas from provider surplus",
                                   spec=pacts.client_budget_transfer_pact(cap="0.05"),
                                   name=f"gastopup-{uuid4().hex[:6]}")
        pact = await pw.wait_pact_active(sub.get("pact_id"))
        async with pw.scoped(pact) as s:
            rid = f"gastopup-{uuid4().hex[:8]}"
            await s.transfer(src_addr=pp.address, dst_addr=cc.address, amount=AMOUNT,
                             token_id=config.NATIVE_TOKEN_ID, chain_id=config.CHAIN_ID,
                             request_id=rid, description="gas top-up for client")
            rec = await s.wait_tx_final(rid, timeout=420.0)
            print("topup tx:", (rec or {}).get("transaction_hash"), "status:", (rec or {}).get("status_display"))


if __name__ == "__main__":
    asyncio.run(main())
