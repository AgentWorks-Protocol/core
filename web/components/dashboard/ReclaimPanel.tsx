"use client";

/** Non-custodial deadline reclaim. When a job the client funded stalled before settlement (still Funded or
 *  Accepted) and its deadline has passed, only the CLIENT can reclaim the escrow (the contract requires
 *  msg.sender == client, so the platform watcher cannot do it). This surfaces that action: it fetches the
 *  claimRefund calldata the client signs with its OWN CAW wallet. Renders nothing unless a job is eligible. */

import { useEffect, useState } from "react";
import { getJobView, getRefundCalldata, type JobView, type CalldataStep } from "../../lib/agent";

function Copy({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button className="filter" style={{ padding: "3px 10px", fontSize: 12 }}
      onClick={async () => { try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1200); } catch { /* noop */ } }}>
      {done ? "copied ✓" : "copy"}
    </button>
  );
}

export function ReclaimPanel({ jobId }: { jobId: number }) {
  const [view, setView] = useState<JobView | null>(null);
  const [cd, setCd] = useState<CalldataStep | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let live = true;
    (async () => { const v = await getJobView(jobId); if (live) setView(v); })();
    return () => { live = false; };
  }, [jobId]);

  if (!view) return null;
  // Only Funded/Accepted jobs are refundable on-chain (a Submitted job is recovered by forceResolve), and
  // only after the deadline. Anything else → render nothing.
  const eligible = ["Funded", "Accepted"].includes(view.on_chain_status) &&
    view.deadline > 0 && view.deadline * 1000 < Date.now();
  if (!eligible) return null;

  async function reclaim() {
    setBusy(true);
    setCd(await getRefundCalldata(jobId));
    setBusy(false);
  }

  return (
    <div className="proof" style={{ marginTop: 16, borderColor: "var(--reclaim)" }}>
      <div className="ph"><span className="t">Deadline passed · reclaim your escrow</span></div>
      <div className="pb" style={{ display: "block" }}>
        <div className="fh" style={{ color: "var(--ink-3)" }}>
          This job stalled before settlement and its deadline has passed. Only you (the client that funded it)
          can reclaim — the platform can&apos;t sign this for you. Fetch the calldata and sign it with your own
          Cobo Agentic Wallet.
        </div>
        {!cd ? (
          <button className="btn accent" style={{ marginTop: 10 }} onClick={reclaim} disabled={busy}>
            {busy ? "Building…" : "Get reclaim calldata"}
          </button>
        ) : (
          <div className="proof" style={{ marginTop: 10 }}>
            <div className="ph"><span className="t">claimRefund · {cd.function}</span><span className="when"><Copy text={cd.calldata} /></span></div>
            <div className="pb" style={{ display: "block" }}>
              <div className="fh" style={{ color: "var(--ink-3)" }}>to {cd.contract_address.slice(0, 10)}… · sign with the client wallet</div>
              <pre style={{ margin: "6px 0 0", padding: 0, overflowX: "auto", fontFamily: "var(--mono)", fontSize: 11, lineHeight: 1.5, background: "none" }}>{cd.calldata}</pre>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
