import { loadMarketRuns } from "../../lib/proofs";
import { listJobsV2 } from "../../lib/chain";
import { HistoryBoard } from "../../components/dashboard/HistoryBoard";
import { irysUrl } from "../../lib/config";
import type { AgentRun } from "../../lib/agent";

export const dynamic = "force-dynamic";

export default async function MarketplacePage() {
  // Seed = verified autonomous run artifacts (3/5/6, snapshotted) joined with any on-chain-only v2 jobs
  // (e.g. an Open/Funded escrow with no artifact). The client then merges live backend /runs on top.
  const artifacts = loadMarketRuns() as AgentRun[];
  const haveArtifact = new Set(artifacts.map((r) => r.job_id));

  const chain = (await listJobsV2()) ?? [];
  const chainOnly: AgentRun[] = chain
    .filter((c) => !haveArtifact.has(c.jobId))
    .map((c) => ({
      run_id: "chain", job_id: c.jobId, txs: {}, accept_decisions: {}, winner: null, winner_addr: null,
      irys: c.irysId ? { id: c.irysId, url: irysUrl(c.irysId) } : null, deliverable: null, verdict: null,
      branch: null, status: "chain", amount_usdc: c.amountUsdc, client: c.client, provider: c.provider,
      final_status: c.statusLabel, content_verified: null,
    }));

  // Deep-normalize em/en-dashes from any LLM-generated run text before it crosses to the client (so even
  // the serialized payload carries none).
  const dedash = <T,>(v: T): T =>
    typeof v === "string" ? (v.replace(/[—–]/g, "-") as unknown as T)
    : Array.isArray(v) ? (v.map(dedash) as unknown as T)
    : v && typeof v === "object" ? (Object.fromEntries(Object.entries(v).map(([k, x]) => [k, dedash(x)])) as unknown as T)
    : v;

  const seed = dedash([...artifacts, ...chainOnly].sort((a, b) => b.job_id - a.job_id));

  return (
    <>
      <div className="head">
        <h1>Marketplace - every settled escrow, on-chain</h1>
        <p>
          The open marketplace, on Ethereum Sepolia: each escrow a client funded, a provider claimed, and a
          committee settled - payout or refund, lifecycle-colored so the whole board reads at a glance. Open one
          for its full on-chain receipt, or head to <strong>Post a job</strong> to open your own escrow.
        </p>
      </div>
      <HistoryBoard seed={seed} />
    </>
  );
}
