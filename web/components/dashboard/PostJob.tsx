"use client";

/** Post a job to the open marketplace — NON-CUSTODIAL. A client names a committee of registered evaluators
 *  and posts with its OWN Cobo Agentic Wallet. Because a CAW wallet is MPC (no browser signing), the primary
 *  path is the client agent's MCP `post_job` tool, which does createJob → approve → fund → publish-listing
 *  atomically with the REAL on-chain job id (so there's no predicted-id race and the task text is always
 *  published). Raw signable calldata is offered as an advanced/manual disclosure. The platform holds no keys. */

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

const srcLabel = (e: DirectoryEntry) =>
  e.source === "self" ? "self-registered" : e.source === "custodial" ? "custodial" : "platform-operated";

export function PostJob() {
  const [evaluators, setEvaluators] = useState<DirectoryEntry[]>([]);
  const [loaded, setLoaded] = useState(false);

  const [client, setClient] = useState("");
  const [task, setTask] = useState("");
  const [criteria, setCriteria] = useState("");
  const [reward, setReward] = useState("5");
  const [picked, setPicked] = useState<Set<string>>(new Set());

  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [calldata, setCalldata] = useState<PostCalldata | null>(null);

  useEffect(() => {
    let live = true;
    (async () => {
      const d = await getDirectory();
      if (!live) return;
      const evals = (d?.participants ?? []).filter((p) => p.role === "evaluator");
      setEvaluators(evals);
      // Default the committee to PLATFORM-OPERATED evaluators (source==='env'), never the first N
      // self-registered rows — otherwise an attacker who seeds evaluator entries gets pre-selected.
      const platform = evals.filter((e) => e.source === "env");
      setPicked(new Set((platform.length ? platform : []).slice(0, 3).map((e) => e.address)));
      setLoaded(true);
    })();
    return () => { live = false; };
  }, []);

  const committee = useMemo(() => [...picked], [picked]);
  const oddOk = committee.length >= 1 && committee.length % 2 === 1;
  const quorum = Math.floor(committee.length / 2) + 1;
  const rewardOk = Number(reward) > 0;
  const ready = task.trim().length > 0 && rewardOk && oddOk;

  function toggle(addr: string) {
    setPicked((s) => { const n = new Set(s); n.has(addr) ? n.delete(addr) : n.add(addr); return n; });
  }

  const mcpCall = useMemo(() => {
    const q = (s: string) => JSON.stringify(s);
    return [
      "// In your MCP client (Claude Desktop / Claude Code) running the AgentWorks server with MCP_ROLE=client:",
      "post_job(",
      `  task=${q(task.trim() || "…")},`,
      `  criteria=${q(criteria.trim())},`,
      `  reward_usdc=${Number(reward) || 0},`,
      `  committee=[${committee.map((a) => q(a)).join(", ")}],`,
      `  quorum=${quorum},`,
      ")",
    ].join("\n");
  }, [task, criteria, reward, committee, quorum]);

  async function genCalldata() {
    setBusy(true); setErr(""); setCalldata(null);
    const res = await getPostCalldata({
      client_address: client.trim(), amount_usdc: Number(reward),
      task: task.trim(), criteria: criteria.trim(), committee: committee.join(","), quorum,
    });
    setBusy(false);
    if (!res.ok || !res.data) { setErr(res.error || "could not build calldata"); return; }
    setCalldata(res.data);
  }

  return (
    <div className="panel sc-body">
      <div className="sc-head" style={{ padding: 0 }}>
        <div>
          <h3>Post a job to the open marketplace</h3>
          <div className="sc-sub">Non-custodial — you post with your own CAW wallet; the platform holds no keys.</div>
        </div>
      </div>

      <div className="lj-form">
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
          <div className="lab">Evaluator committee · pick an odd number{committee.length ? ` (quorum ${quorum})` : ""}</div>
          {!loaded ? (
            <div className="empty">Loading evaluators…</div>
          ) : evaluators.length === 0 ? (
            <div className="err">
              No evaluators available. A job needs a committee to settle it — an evaluator must
              <Link href="/dashboard/register"> register</Link> first.
            </div>
          ) : (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              {evaluators.map((e) => (
                <button key={e.address} className={`filter${picked.has(e.address) ? " on" : ""}`} onClick={() => toggle(e.address)}
                  title={`${srcLabel(e)}${e.llm_model ? " · " + e.llm_model : ""}`}>
                  {e.name} · {shortHex(e.address)} <span style={{ opacity: 0.6 }}>· {srcLabel(e)}</span>
                </button>
              ))}
            </div>
          )}
          {loaded && evaluators.length > 0 && !oddOk && (
            <p className="lj-note">Select an odd number of evaluators (1, 3, 5…) so a quorum is unambiguous. Prefer platform-operated or evaluators you trust.</p>
          )}
        </div>
      </div>

      {/* Primary path — MCP post_job (atomic, real id, auto-publishes the listing) */}
      {ready && (
        <div className="proof" style={{ marginTop: 8 }}>
          <div className="ph"><span className="t">Post with your client agent (recommended)</span><span className="when"><Copy text={mcpCall} /></span></div>
          <pre style={{ margin: 0, padding: 14, overflowX: "auto", fontFamily: "var(--mono)", fontSize: 12, lineHeight: 1.5 }}>{mcpCall}</pre>
          <div className="pb" style={{ display: "block" }}>
            <div className="fh" style={{ color: "var(--ink-3)" }}>
              One step: createJob → approve → fund → publish listing, using the real on-chain job id. Set up your
              client agent on <Link href="/dashboard/register">Register agent</Link>.
            </div>
          </div>
        </div>
      )}

      {/* Advanced — raw signable calldata */}
      <div style={{ marginTop: 14 }}>
        <button className="filter" onClick={() => setAdvanced((v) => !v)}>{advanced ? "▾" : "▸"} Advanced: raw signable calldata</button>
        {advanced && (
          <div style={{ marginTop: 10 }}>
            <div className="field">
              <div className="lab">Your client wallet address</div>
              <input className="inp" placeholder="0x… (the CAW wallet that funds the escrow)" value={client} onChange={(e) => setClient(e.target.value)} />
            </div>
            <button className="btn accent" onClick={genCalldata} disabled={!ready || !client.trim().startsWith("0x") || busy}>
              {busy ? "Building…" : "Generate createJob / approve / fund calldata"}
            </button>
            {err && <div className="err" style={{ marginTop: 10 }}>⚠ {err}</div>}
            {calldata && (
              <>
                <div className="err" style={{ marginTop: 12, background: "var(--paper-2)", borderColor: "var(--line)", color: "var(--ink-2)" }}>
                  Sign these three with your own CAW wallet, in order. The job id below is <b>predicted</b> — if
                  another job is created first the real id differs, so after funding, read the <b>createJob
                  receipt</b> for the actual id, then publish your listing (<code>POST /marketplace/jobs</code>
                  with the confirmed id) so providers can discover the task. The MCP path above does all of this
                  atomically — prefer it unless you have your own signer.
                </div>
                {calldata.steps.map((s) => (
                  <div className="proof" key={s.step} style={{ marginTop: 12 }}>
                    <div className="ph"><span className="t">{s.step} · {s.function}</span><span className="when"><Copy text={s.calldata} /></span></div>
                    <div className="pb" style={{ display: "block" }}>
                      <div className="fh" style={{ color: "var(--ink-3)" }}>to {shortHex(s.to, 10)} · predicted job #{calldata.predicted_job_id}</div>
                      <pre style={{ margin: "6px 0 0", padding: 0, overflowX: "auto", fontFamily: "var(--mono)", fontSize: 11, lineHeight: 1.5, background: "none" }}>{s.calldata}</pre>
                    </div>
                  </div>
                ))}
              </>
            )}
          </div>
        )}
      </div>

      <div className="lj-section" style={{ marginTop: 22 }}>
        <div className="lj-sh">Registered evaluators settle jobs · discovery only — enforcement is the Pact + escrow</div>
      </div>
    </div>
  );
}
