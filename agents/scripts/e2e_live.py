"""Live end-to-end drive of the reworked marketplace (production-registration fixes).

Posts ONE real job as the client (naming the platform evaluator committee), then runs the STANDING
provider + committee services + the settlement watcher exactly as the deployed service would — and monitors
until the job settles on-chain. Proves: standing platform agents claim + vote + finalize a job outside any
demo run, on the hardened v4 escrow. Signing routes through the operator's CAW TSS node via the relay.

Run:  agents/.venv/Scripts/python.exe agents/scripts/e2e_live.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # import sibling agent modules

import config
import registry
import pacts
import escrow_v4 as esc
import autonomous
from caw import CawWallet
from web3 import Web3

TASK = "In one clear sentence, explain what a sealed commit-reveal accept race prevents in an open job marketplace."
CRITERIA = "One sentence; names front-running / MEV of the accept."
REWARD_USDC = 1.0


async def post_job(committee: list[str], quorum: int) -> int:
    cc = config.client_agent()
    w3 = esc.web3()
    amt = int(round(REWARD_USDC * 1_000_000))
    async with CawWallet(api_url=config.CAW_API_URL, api_key=cc.api_key,
                         wallet_uuid=cc.wallet_id, name="Client") as cw:
        await autonomous._revoke_active(cw)
        sub = await cw.submit_pact(
            intent="Client funds an open marketplace job",
            spec=pacts.client_escrow_pact(escrow=config.ESCROW_V4_ADDRESS, usdc=config.USDC_ADDRESS),
            name=f"e2e-client-{int(time.time())}")
        pact = await cw.wait_pact_active(sub.get("pact_id"))
        async with cw.scoped(pact) as client:
            job_id = esc.next_job_id(w3)
            spec = f"{TASK}\n\nAcceptance criteria: {CRITERIA}"
            spec_hash = Web3.keccak(text=f"{spec}#{job_id}")
            deadline = int(time.time()) + 7 * 24 * 3600
            print(f"[post] job #{job_id} committee={[c[:8] for c in committee]} quorum={quorum} reward={REWARD_USDC} USDC", flush=True)
            tx = await autonomous._call(client, cc.address, config.ESCROW_V4_ADDRESS,
                esc.create_job(committee, quorum, amt, spec_hash, deadline), "createJob")
            print("[post] createJob", tx, flush=True)
            tx = await autonomous._call(client, cc.address, config.USDC_ADDRESS,
                esc.approve(config.ESCROW_V4_ADDRESS, amt), "approve")
            print("[post] approve", tx, flush=True)
            tx = await autonomous._call(client, cc.address, config.ESCROW_V4_ADDRESS,
                esc.fund(job_id), "fund")
            print("[post] fund", tx, flush=True)
            autonomous._post_listing(job_id, task=TASK, criteria=CRITERIA, reward_usdc=REWARD_USDC,
                spec_hash=Web3.to_hex(spec_hash), client=cc.address, deadline=deadline)
            print(f"[post] listing published for job #{job_id}", flush=True)
            return job_id


async def main() -> None:
    committee = [m.address for m in registry.evaluators() if not m.is_self_driven][:config.COMMITTEE_SIZE]
    quorum = config.COMMITTEE_QUORUM
    if len(committee) < quorum:
        raise SystemExit(f"need >= {quorum} platform evaluators, have {len(committee)}")

    job_id = await post_job(committee, quorum)

    stop = asyncio.Event()
    run = autonomous.Run(mode="live", target=10 ** 9)
    run.stop = stop
    w3 = esc.web3()
    t0 = time.time()

    async def monitor() -> None:
        last = None
        while not stop.is_set():
            await asyncio.sleep(15)
            try:
                job = await asyncio.to_thread(esc.get_job, w3, job_id)
            except Exception as e:
                print(f"[monitor] read err {type(e).__name__}", flush=True)
                continue
            v = None
            try:
                v = await asyncio.to_thread(esc.get_vote, w3, job_id)
            except Exception:
                pass
            tally = f" votes {v['approve']}-{v['reject']}" if v else ""
            if job["status"] != last:
                print(f"[monitor] #{job_id} status={job['status']}{tally}  (+{int(time.time()-t0)}s)", flush=True)
                last = job["status"]
            if job["status"] in ("Completed", "Rejected", "Refunded"):
                print(f"[monitor] SETTLED #{job_id} -> {job['status']} (+{int(time.time()-t0)}s)", flush=True)
                stop.set()
                return
            if time.time() - t0 > 2400:  # 40 min safety cap
                print("[monitor] timed out after 40 min", flush=True)
                stop.set()
                return

    await asyncio.gather(
        autonomous.provider_service(run),
        autonomous.committee_service(run),
        autonomous.settlement_watcher(stop=stop, poll=12.0),
        monitor(),
    )

    job = await asyncio.to_thread(esc.get_job, w3, job_id)
    print("\n=== FINAL ===", flush=True)
    print(f"job #{job_id}: {job['status']} | provider {job['provider']} | irys {job['irys_id']}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
