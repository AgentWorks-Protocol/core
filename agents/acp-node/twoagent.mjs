/**
 * ACP two-agent end-to-end proof (Base Sepolia). Provider = 2nd registered agent; Buyer+Evaluator = our agent
 * (self-evaluation). One process runs both. Full flow:
 *   buyer.createJob(provider=2nd, evaluator=self) -> requirement -> provider.setBudget -> buyer.fund ->
 *   provider.submit -> job.submitted -> committee verdict (/acp/verdict) -> complete/reject -> completed/rejected.
 * Usage: node --env-file=.env twoagent.mjs [good|bad]
 */
import { AcpAgent, PrivyAlchemyEvmProviderAdapter, AssetToken } from "@virtuals-protocol/acp-node-v2";
import { baseSepolia } from "viem/chains";

const E = process.env;
const CHAIN = baseSepolia.id;
const CASE = (process.argv[2] || "good").toLowerCase();
const SPEC = "Summarize what an on-chain escrow does, in 2 sentences, for a non-expert.";
const DELIVERABLE = CASE === "bad"
  ? "gm frens escrow is bullish wagmi buy now"
  : "An escrow is a neutral smart contract that holds funds until agreed conditions are met, then releases them. Because neither party can grab the money unilaterally, strangers can transact safely without trusting each other.";

async function committeeVerdict(spec, deliverable) {
  const r = await fetch(`${E.AGENT_API.replace(/\/$/, "")}/acp/verdict`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ spec, deliverable }),
  });
  if (!r.ok) throw new Error(`verdict ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return r.json();
}

const adapter = (walletAddress, walletId, signerPrivateKey) =>
  PrivyAlchemyEvmProviderAdapter.create({ walletAddress, walletId, signerPrivateKey, chains: [baseSepolia] });

async function main() {
  const provider = await AcpAgent.create({ evmProvider: await adapter(E.SECOND_ADDRESS, E.SECOND_ID, E.SECOND_PRIVATE_KEY) });
  const buyer = await AcpAgent.create({ evmProvider: await adapter(E.WALLET_ADDRESS, E.WALLET_ID, E.SIGNER_PRIVATE_KEY) });
  const providerAddr = await provider.getAddress();
  const buyerAddr = await buyer.getAddress();
  let done = false;

  provider.on("entry", async (session, entry) => {
    try {
      if (entry.kind === "message" && entry.contentType === "requirement" && session.status === "open") {
        console.log("[provider] requirement -> setBudget(0.1)");
        await session.setBudget(AssetToken.usdc(0.1, session.chainId));
      } else if (entry.kind === "system" && entry.event.type === "job.funded") {
        console.log(`[provider] funded -> submit (${CASE} deliverable)`);
        await session.submit(DELIVERABLE);
      }
    } catch (e) { console.error("[provider]", e?.stack ?? e?.message ?? e); }
  });

  buyer.on("entry", async (session, entry) => {
    try {
      if (entry.kind !== "system") return;
      switch (entry.event.type) {
        case "budget.set":
          console.log("[buyer] budget.set -> fund(0.1)");
          await session.fund(AssetToken.usdc(0.1, session.chainId));
          break;
        case "job.submitted": {
          console.log("[evaluator] job.submitted -> asking the committee…");
          const v = await committeeVerdict(SPEC, entry.event.deliverable);
          const reason = `AgentWorks committee ${v.accept ? "APPROVE" : "REJECT"} (${v.approve}/${v.quorum} of ${v.approve + v.reject})`;
          console.log(`[evaluator] accept=${v.accept} -> ${v.accept ? "complete" : "reject"} | ${reason}`);
          if (v.accept) await session.complete(reason); else await session.reject(reason);
          break;
        }
        case "job.completed":
        case "job.rejected":
          console.log(`\nRESULT: job ${session.jobId} -> ${entry.event.type.toUpperCase()}  (case=${CASE})`);
          done = true;
          await Promise.allSettled([buyer.stop(), provider.stop()]);
          process.exit(0);
      }
    } catch (e) { console.error("[buyer]", e?.stack ?? e?.message ?? e); }
  });

  await provider.start(() => console.log(`[start] provider ${providerAddr} online`));
  await buyer.start(() => console.log(`[start] buyer/evaluator ${buyerAddr} online`));

  const expiredAt = Math.floor(Date.now() / 1000) + 3600;
  const jobId = await buyer.createJob(CHAIN, {
    providerAddress: providerAddr, evaluatorAddress: buyerAddr, expiredAt, description: "escrow-explainer",
  });
  console.log(`[buyer] created job ${jobId} (provider=${providerAddr.slice(0, 10)}…, evaluator=self)`);
  const session = buyer.getSession(CHAIN, String(jobId));
  await session.sendMessage(JSON.stringify({ requirement: SPEC }), "requirement");
  console.log("[buyer] requirement sent");

  setTimeout(() => { if (!done) { console.error("TIMEOUT — did not complete in 4m"); process.exit(1); } }, 240000);
}

main().catch((e) => { console.error(e?.stack ?? e); process.exit(1); });
