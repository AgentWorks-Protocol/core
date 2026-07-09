"use client";

/** Post a job to the open marketplace — NON-CUSTODIAL. A registered client enters the task + reward and
 *  names a committee of registered evaluators; the platform returns the createJob / approve / fund calldata,
 *  which the client signs with its OWN CAW wallet (via MCP or its own tooling). The platform never holds the
 *  client's keys and never drives the wallet — it only encodes. Any registered provider can then claim the
 *  job and the committee settles it. */

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { getDirectory, getPostCalldata, type DirectoryEntry, type PostCalldata } from "../../lib/agent";
import { shortHex } from "../../lib/config";

function Copy({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button className="filter" style={{ padding: "3px 10px", fontSize: 12 }}
      onClick={async () => { try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1200); } catch { /* noop */ } }}>
      {done ? "copied ✓" : "copy"}
    </button>
  );
}

export function PostJob() {
  const [evaluators, setEvaluators] = useState<DirectoryEntry[]>([]);
  const [loaded, setLoaded] = useState(false);

  const [client, setClient] = useState("");
  const [task, setTask] = useState("");
  const [criteria, setCriteria] = useState("");
  const [reward, setReward] = useState("5");
  const [picked, setPicked] = useState<Set<string>>(new Set());

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [result, setResult] = useState<PostCalldata | null>(null);

  useEffect(() => {
    let live = true;
    (async () => {
      const d = await getDirectory();
      if (!live) return;
      const evals = (d?.participants ?? []).filter((p) => p.role === "evaluator");
      setEvaluators(evals);
      setPicked(new Set(evals.slice(0, 3).map((e) => e.address))); // preselect up to 3
      setLoaded(true);
    })();
    return () => { live = false; };
  }, []);

  const committee = useMemo(() => [...picked], [picked]);
  const oddOk = committee.length >= 1 && committee.length % 2 === 1;
  const quorum = Math.floor(committee.length / 2) + 1;
  const valid = client.trim().startsWith("0x") && task.trim().length > 0 && Number(reward) > 0 && oddOk;

  function toggle(addr: string) {
    setPicked((s) => { const n = new Set(s); n.has(addr) ? n.delete(addr) : n.add(addr); return n; });
  }

  async function generate() {
    setBusy(true); setErr(""); setResult(null);
    const res = await getPostCalldata({
      client_address: client.trim(), amount_usdc: Number(reward),
      task: task.trim(), criteria: criteria.trim(), committee: committee.join(","), quorum,
    });
    setBusy(false);
    if (!res.ok || !res.data) { setErr(res.error || "could not build calldata"); return; }
    setResult(res.data);
  }

  return (
    <div className="panel sc-body">
      <div className="sc-head" style={{ padding: 0 }}>
        <div>
          <h3>Post a job to the open marketplace</h3>
          <div className="sc-sub">Non-custodial — the platform encodes the calldata; you sign with your own CAW wallet.</div>
        </div>
      </div>

      {result ? (
        <>
          <div className="err" style={{ background: "var(--paper-2)", borderColor: "var(--line)", color: "var(--ink-2)" }}>
            Predicted job <b>#{result.predicted_job_id}</b> · {result.amount_usdc} USDC · committee {result.committee.length} (quorum {result.quorum}).
            Sign the three calls below with your own CAW wallet, in order. The platform holds none of your keys.
          </div>
          {result.steps.map((s) => (
            <div className="proof" key={s.step} style={{ marginTop: 12 }}>
              <div className="ph"><span className="t">{s.step} · {s.function}</span><span className="when"><Copy text={s.calldata} /></span></div>
              <div className="pb" style={{ display: "block" }}>
                <div className="fh" style={{ color: "var(--ink-3)" }}>to {shortHex(s.to, 10)}</div>
                <pre style={{ margin: "6px 0 0", padding: 0, overflowX: "auto", fontFamily: "var(--mono)", fontSize: 11, lineHeight: 1.5, background: "none" }}>{s.calldata}</pre>
              </div>
            </div>
          ))}
          <p className="lj-note" style={{ marginTop: 14 }}>
            Easiest path: your registered client agent can do all of this in one step with the MCP
            <code> post_job</code> tool (createJob → approve → fund → publish listing). After funding, verify the
            real job id from the createJob receipt before publishing the listing (the id above is predicted).
          </p>
          <div className="sc-actions" style={{ padding: "8px 0 0", background: "none", border: 0 }}>
            <button className="btn" onClick={() => setResult(null)}>Post another</button>
            <Link className="btn primary" href="/dashboard">View the marketplace →</Link>
          </div>
        </>
      ) : (
        <div className="lj-form">
          <div className="field">
            <div className="lab">Your client wallet address</div>
            <input className="inp" placeholder="0x… (the CAW wallet that will fund the escrow)" value={client} onChange={(e) => setClient(e.target.value)} />
          </div>
          <div className="field">
            <div className="lab">Task</div>
            <input className="inp big" placeholder="What you want an agent to do" value={task} onChange={(e) => setTask(e.target.value)} />
          </div>
          <div className="field">
            <div className="lab">Acceptance criteria</div>
            <textarea className="inp area" placeholder="What the evaluator committee checks for" value={criteria} onChange={(e) => setCriteria(e.target.value)} />
          </div>
          <div className="field">
            <div className="lab">Reward (USDC)</div>
            <div className="amtin">
              <input className="v" inputMode="decimal" value={reward}
                onChange={(e) => setReward(e.target.value.replace(/[^0-9.]/g, ""))}
                style={{ width: 90, border: 0, background: "transparent", fontFamily: "var(--mono)", fontWeight: 600, fontSize: 18, color: "var(--ink)" }} />
              <span className="u">USDC</span>
            </div>
          </div>
          <div className="field">
            <div className="lab">Evaluator committee · pick an odd number of registered evaluators{committee.length ? ` (quorum ${quorum})` : ""}</div>
            {!loaded ? (
              <div className="empty">Loading registered evaluators…</div>
            ) : evaluators.length === 0 ? (
              <div className="err">
                No evaluators registered yet. A job needs a committee to settle it — ask an evaluator to
                <Link href="/dashboard/register"> register</Link> first.
              </div>
            ) : (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {evaluators.map((e) => (
                  <button key={e.address} className={`filter${picked.has(e.address) ? " on" : ""}`} onClick={() => toggle(e.address)}>
                    {e.name} · {shortHex(e.address)}
                  </button>
                ))}
              </div>
            )}
            {loaded && evaluators.length > 0 && !oddOk && (
              <p className="lj-note">Select an odd number of evaluators (1, 3, 5…) so a quorum is unambiguous.</p>
            )}
          </div>
          <button className="btn accent lj-go" onClick={generate} disabled={!valid || busy}>
            {busy ? "Building calldata…" : "Generate signable calldata →"}
          </button>
          {err && <div className="err">⚠ {err}</div>}
        </div>
      )}
    </div>
  );
}
