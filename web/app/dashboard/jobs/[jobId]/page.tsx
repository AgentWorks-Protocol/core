import Link from "next/link";
import { notFound } from "next/navigation";
import { findMarketRun } from "../../../../lib/proofs";
import { liveJobV2, settlementV2 } from "../../../../lib/chain";
import { CFG, txUrl, irysUrl, addrUrl, shortHex } from "../../../../lib/config";
import { Badge } from "../../../../components/Badge";
import { runBadge } from "../../../../components/dashboard/RunCard";
import type { AgentRun } from "../../../../lib/agent";

export const dynamic = "force-dynamic";

const STEP_DEFS: { key: string; ti: string }[] = [
  { key: "createJob", ti: "Job created (open, no provider)" },
  { key: "approve", ti: "USDC approved" },
  { key: "fund", ti: "USDC escrowed" },
  { key: "commitAccept", ti: "Sealed bid committed (jobId hidden)" },
  { key: "revealAccept", ti: "Provider won the sealed race → claimed" },
  { key: "acceptJob", ti: "Provider won the race → claimed" }, // legacy v2 artifacts
  { key: "submitWork", ti: "Work submitted + Irys id anchored" },
  { key: "finalize", ti: "Committee resolved → finalized (v4)" },
  { key: "complete", ti: "Accepted → Provider paid" }, // legacy v2/v3 single-evaluator artifacts
  { key: "reject", ti: "Rejected → Client refunded" },
];

const check = (
  <svg width="11" height="11" viewBox="0 0 12 12"><path d="M3 6.5l2 2 4-5" fill="none" stroke="var(--settled)" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>
);

export default async function JobDetailPage({ params }: { params: Promise<{ jobId: string }> }) {
  const { jobId: jobIdStr } = await params;
  const jobId = Number(jobIdStr);
  if (!Number.isFinite(jobId)) notFound();

  // Prefer the verified autonomous-run artifact (rich: decisions + race + every tx); fall back to a
  // minimal on-chain v2 view so any escrow still resolves.
  const run = findMarketRun(jobId) as AgentRun | null;
  const live = await liveJobV2(jobId);
  if (!run && !live) notFound();

  const clean = (s?: string | null) => (s ? s.replace(/[—–]/g, "-") : s); // normalize LLM dashes for display
  const amount = run?.amount_usdc ?? live?.amountUsdc ?? 0;
  const statusLabel = live?.statusLabel ?? run?.final_status ?? "-";
  const badge = run ? runBadge(run) : runBadge({ job_id: jobId, final_status: live!.statusLabel } as AgentRun);
  const title = clean(run?.task) ?? `Escrow job #${jobId}`;
  const provider = run?.provider ?? live?.provider ?? "";
  const accepts = Object.entries(run?.accept_decisions ?? {});
  const raced = accepts.length > 1;
  const steps = STEP_DEFS.filter((s) => run?.txs?.[s.key]);
  // The settling tx from the artifact: v4 finalize/claimRefund, or legacy v2/v3 complete/reject.
  const artifactSettleTx = run?.txs?.complete || run?.txs?.reject || run?.txs?.finalize || run?.txs?.claimRefund;
  // For jobs whose artifact has no settlement yet (or no artifact at all, e.g. MCP-settled), recover the
  // settling tx + outcome from chain events across both v4 escrows.
  const chainSettle = !artifactSettleTx ? await settlementV2(jobId) : null;
  const settleTx = artifactSettleTx || chainSettle?.txHash || "";
  // Outcome: prefer the artifact's branch, else derive from the on-chain status (Completed→payout, Rejected/Refunded→refund).
  const settled = statusLabel === "Completed" ? "payout" : statusLabel === "Rejected" || statusLabel === "Refunded" ? "refund" : null;
  const outcome = run?.branch ?? chainSettle?.outcome ?? settled;
  const outcomeLabel = outcome === "payout" ? "Provider paid" : outcome === "refund" ? "Client refunded" : "-";
  const irys = run?.irys ?? (live?.irysId ? { id: live.irysId, url: irysUrl(live.irysId) } : null);

  return (
    <>
      <div className="head">
        <h1 style={{ fontSize: 26 }}>Escrow · Job #{jobId}</h1>
        <p style={{ maxWidth: "70ch" }}>{title}</p>
      </div>

      <div className="panel sc-body">
        <div className="sc-head" style={{ padding: 0 }}>
          <div><h3>Settlement detail</h3><div className="sc-sub">Open marketplace · {amount.toFixed(2)} USDC</div></div>
          <Badge state={badge.state} label={statusLabel} />
        </div>

        <div className="agents">
          <div className="agent">
            <div className="role">Client</div>
            <div className="addr"><span style={{ width: 7, height: 7, borderRadius: "50%", background: "var(--settle)" }} /><a href={addrUrl(CFG.clientCaw)} target="_blank" rel="noreferrer">{shortHex(CFG.clientCaw)}</a></div>
            <div className="pact">Pact · escrow v4 + USDC allowlist</div>
          </div>
          <div className="seam" />
          <div className="agent">
            <div className="role">Provider {run?.winner ? `· ${run.winner}` : ""}</div>
            <div className="addr"><span style={{ width: 7, height: 7, borderRadius: "50%", background: "var(--work)" }} /><a href={addrUrl(provider || CFG.providerCaw)} target="_blank" rel="noreferrer">{shortHex(provider || CFG.providerCaw)}</a></div>
            <div className="pact">Pact · escrow v4 allowlist (no USDC)</div>
          </div>
        </div>

        {(run?.fund_decision || accepts.length > 0 || run?.verdict) && (
          <div className="reason">
            {run?.fund_decision && (
              <div className="rcard"><div className="rk">Client · fund decision (LLM)</div>
                <div className={`verdict ${run.fund_decision.fund ? "y" : "n"}`}>{run.fund_decision.fund ? "FUND ✓" : "DECLINE ✕"}</div>
                <div className="why">{clean(run.fund_decision.reason)}</div></div>
            )}
            {accepts.map(([who, d]) => (
              <div className="rcard" key={who}><div className="rk">{who} · accept {raced ? (who === run?.winner ? "· won race" : "· lost race") : ""}</div>
                <div className={`verdict ${d.accept ? "y" : "n"}`}>{d.accept ? "ACCEPT ✓" : "PASS ✕"}</div>
                <div className="why">{raced && who !== run?.winner ? "acceptJob reverted - " : ""}{clean(d.reason)}</div></div>
            ))}
            {run?.verdict && (
              <div className="rcard"><div className="rk">Evaluator · verdict (LLM)</div>
                <div className={`verdict ${run.verdict.accept ? "y" : "n"}`}>{run.verdict.accept ? "ACCEPT ✓ → payout" : "REJECT ✕ → refund"}</div>
                <div className="why">{clean(run.verdict.reason)}</div></div>
            )}
          </div>
        )}

        {steps.length > 0 && (
          <div className="timeline">
            {steps.map((s, i) => (
              <div key={s.key} className="tstep done">
                <div className="mk"><span className="o" />{i < steps.length - 1 && <span className="ln" />}</div>
                <div><div className="ti">{s.ti}</div><div className="td">{check}{s.key}() · <a className="lnk" href={txUrl(run!.txs[s.key])} target="_blank" rel="noreferrer">{shortHex(run!.txs[s.key], 10)}</a></div></div>
                <div className="tt" />
              </div>
            ))}
          </div>
        )}

        {irys && (
          <div className="proof" style={{ marginTop: 18 }}>
            <div className="ph"><span className="t">Deliverable · Irys</span><span className="when"><a className="lnk" href={irysUrl(irys.id)} target="_blank" rel="noreferrer">{shortHex(irys.id, 10)}</a></span></div>
            <div className="pb">
              <div className="ic"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M4 7h16M4 12h16M4 17h10" /></svg></div>
              <div><div className="fn">deliverable.txt</div>{run?.deliverable && <div className="fh" style={{ color: "var(--ink-3)" }}>{clean(run.deliverable.slice(0, 90))}…</div>}</div>
              {run?.content_verified && <span className="verified" style={{ marginLeft: "auto" }}>{check} hash verified</span>}
            </div>
          </div>
        )}

        <div className="receipt" style={{ marginTop: 18 }}>
          <div className="rcell"><div className="rk">Outcome</div><div className="rv">{outcomeLabel}</div></div>
          <div className="rcell"><div className="rk">Amount</div><div className="rv">{amount.toFixed(2)} USDC</div></div>
          <div className="rcell"><div className="rk">settle() tx</div><div className="rv">{settleTx ? <a href={txUrl(settleTx)} target="_blank" rel="noreferrer">{shortHex(settleTx, 10)}</a> : "-"}</div></div>
          <div className="rcell"><div className="rk">Final status</div><div className="rv">{statusLabel}</div></div>
        </div>

        <div className="sc-actions" style={{ padding: "18px 0 0", background: "none", border: 0 }}>
          <Link className="btn" href="/dashboard">← Back to Marketplace</Link>
          {settleTx && <a className="btn primary" href={txUrl(settleTx)} target="_blank" rel="noreferrer">View on explorer</a>}
        </div>
      </div>
    </>
  );
}
