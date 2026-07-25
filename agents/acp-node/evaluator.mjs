/**
 * AgentWorks ACP evaluator agent (Model A).
 *
 * Registers as an ACP evaluator; on every `job.submitted` where we're the evaluator, it reads the job's
 * requirement (spec) + submitted deliverable, asks the AgentWorks M-of-N committee for a verdict (the Python
 * /acp/verdict service), and settles the ACP job: accept -> session.complete (pay provider), else
 * session.reject (refund). To ACP it's one confident evaluator; underneath, N independent LLM personas judge
 * and a quorum decides. See docs/ACP_ADAPTER.md.
 *
 * API confirmed against @virtuals-protocol/acp-node-v2@0.1.9 dist types:
 *   AcpAgent.create({ evmProvider }) · PrivyAlchemyEvmProviderAdapter.create({ walletAddress, walletId,
 *   signerPrivateKey, chains }) · agent.on("entry", (session, entry) => …) · agent.start()
 *   SystemEntry { kind:"system", event: { type:"job.submitted", deliverable } } ·
 *   AgentMessage { kind:"message", contentType:"requirement", content } · session.entries / session.roles /
 *   session.jobId · session.complete(reason) / session.reject(reason)
 */

import { AcpAgent, PrivyAlchemyEvmProviderAdapter } from "@virtuals-protocol/acp-node-v2";
import { baseSepolia } from "viem/chains";

const {
  WALLET_ADDRESS,
  WALLET_ID,
  SIGNER_PRIVATE_KEY,
  AGENT_API,
  ACP_VERDICT_TOKEN = "",
} = process.env;

for (const [k, v] of Object.entries({ WALLET_ADDRESS, WALLET_ID, SIGNER_PRIVATE_KEY, AGENT_API })) {
  if (!v) { console.error(`missing env: ${k} (see .env.example)`); process.exit(1); }
}

/** Ask the AgentWorks committee for a verdict. Returns {accept, approve, reject, quorum, reasons[]}. */
async function committeeVerdict(spec, deliverable) {
  const r = await fetch(`${AGENT_API.replace(/\/$/, "")}/acp/verdict`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(ACP_VERDICT_TOKEN ? { authorization: `Bearer ${ACP_VERDICT_TOKEN}` } : {}),
    },
    body: JSON.stringify({ spec, deliverable }),
  });
  if (!r.ok) throw new Error(`verdict service ${r.status}: ${(await r.text()).slice(0, 300)}`);
  return r.json();
}

/** The client's requirement (spec) is a room message with contentType "requirement" (latest wins). */
function specFromSession(session) {
  const req = [...session.entries].reverse()
    .find((e) => e.kind === "message" && e.contentType === "requirement");
  return req?.content ?? "";
}

async function main() {
  const evmProvider = await PrivyAlchemyEvmProviderAdapter.create({
    walletAddress: WALLET_ADDRESS,
    walletId: WALLET_ID,
    signerPrivateKey: SIGNER_PRIVATE_KEY,
    chains: [baseSepolia],
  });
  const agent = await AcpAgent.create({ evmProvider });

  agent.on("entry", async (session, entry) => {
    try {
      // Act only when a provider has just submitted work on a job where we're the evaluator.
      if (entry.kind !== "system" || entry.event?.type !== "job.submitted") return;
      if (!session.roles.includes("evaluator")) return;

      const deliverable = entry.event.deliverable ?? "";
      const spec = specFromSession(session);
      if (!spec || !deliverable) {
        console.warn(`[acp] job ${session.jobId}: missing ${!spec ? "spec" : "deliverable"} — skipping`);
        return;
      }

      const v = await committeeVerdict(spec, deliverable);
      const tally = `${v.approve}/${v.quorum} of ${v.approve + v.reject}`;
      const reason = `AgentWorks committee ${v.accept ? "APPROVE" : "REJECT"} (${tally}) · ` +
        (v.reasons ?? []).map((x) => `${x.member}:${x.accept ? "✓" : "✗"}`).join(" ");

      if (v.accept) await session.complete(reason);
      else await session.reject(reason);
      console.log(`[acp] job ${session.jobId} -> ${v.accept ? "COMPLETE" : "REJECT"} | ${reason}`);
    } catch (e) {
      console.error(`[acp] job ${session?.jobId ?? "?"} handler failed:`, e?.message ?? e);
    }
  });

  await agent.start(() => {
    console.log(`[acp] AgentWorks evaluator online as ${WALLET_ADDRESS} (Base Sepolia) — verdicts via ${AGENT_API}`);
  });
}

main().catch((e) => { console.error(e); process.exit(1); });
