"""Prove the hosted TSS signer co-signs, end-to-end through the CAW relay.

Fires a small native SETH transfer Provider -> deployer gas hub. A returned tx hash proves the
droplet-hosted TSS node (not a local process) produced the signature; on the signer host,
`docker logs agentworks-signer` shows the matching `SessionTypeSigning ... completed` entry. Dual
purpose: replenishes the deployer hub. See docs/DEPLOY_SIGNER.md.

Usage: python scripts/verify_hosted_sign.py [amount_seth]
"""
from __future__ import annotations

import asyncio, sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config, pacts
from caw.client import CawWallet

DEPLOYER = "0xBCA6f82e240C6AC36B23b4f7D21adF17e03966Fe"


async def main() -> None:
    amount = sys.argv[1] if len(sys.argv) > 1 else "0.004"
    pp = config.provider_agent()
    print(f"hosted-signer check: Provider {pp.address} -> deployer hub, {amount} {config.NATIVE_TOKEN_ID}")
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
        sub = await pw.submit_pact(intent="Verify hosted signer + replenish deployer gas hub",
                                   spec=pacts.client_budget_transfer_pact(cap="0.05"),
                                   name=f"hostverify-{uuid4().hex[:6]}")
        pact = await pw.wait_pact_active(sub.get("pact_id"))
        async with pw.scoped(pact) as s:
            rid = f"hostverify-{uuid4().hex[:8]}"
            await s.transfer(src_addr=pp.address, dst_addr=DEPLOYER, amount=amount,
                             token_id=config.NATIVE_TOKEN_ID, chain_id=config.CHAIN_ID,
                             request_id=rid, description="hosted-signer verification transfer")
            rec = await s.wait_tx_final(rid, timeout=600.0)
            print("SIGNED tx:", (rec or {}).get("transaction_hash"), "status:", (rec or {}).get("status_display"))


if __name__ == "__main__":
    asyncio.run(main())
