import { loadBeats, loadPacts } from "../../../lib/proofs";
import { CFG, txUrl, shortHex } from "../../../lib/config";
import { DirectoryPanel } from "../../../components/dashboard/DirectoryPanel";
import Link from "next/link";

export const dynamic = "force-dynamic";

export default function ProofsPage() {
  const beats = loadBeats();
  const pacts = loadPacts();

  return (
    <>
      <div className="head">
        <h1>Proofs - the load-bearing trust layer</h1>
        <p>
          A Pact is enforced server-side by the Cobo Agentic Wallet - an agent cannot exceed it regardless of what its
          LLM decides. These three beats and the literal Pact policies are the risk-boundary evidence, captured from
          verified CAW runs.
        </p>
      </div>

      {/* participants / wallet-onboarding boundary — LIVE from the registry directory */}
      <div className="lj-sh" style={{ marginBottom: 10 }}>Registered agents · each bound by a scoped Pact in its own Cobo Agentic Wallet</div>
      <DirectoryPanel />
      <p className="pact-onboard">
        Each agent is onboarded into its own Cobo Agentic Wallet and bound to a scoped Pact at submit time
        (a pact-scoped API key carries the authority). The provider Pact omits USDC entirely - a provider can
        accept and deliver but can never move escrowed funds; only the escrow contract settles. New participants
        join non-custodially via <Link href="/dashboard/register">Register agent</Link> — they bring their own
        wallet, self-bind the same template Pact, and the authority boundary travels with them.
      </p>

      <div className="beats">
        {/* DENIAL */}
        <div className="beat">
          <div className="bh"><span className="bnum">BEAT 01</span><h3>Pact denial</h3><span className="denied-pill">403 DENIED</span></div>
          <p>An over-budget transfer and a call to a non-allowlisted contract are both refused before reaching the chain.</p>
          {beats.denials.map((d) => (
            <div className="codeline" key={d.code}><span className="c">{d.code}</span> · {d.statusCode}<br />{d.detail}</div>
          ))}
          {beats.auditDenied.length > 0 && (
            <div className="audit">
              {beats.auditDenied.slice(0, 4).map((e, i) => (
                <div className="ae" key={i}><span className="x">✕</span> {e.action} · {e.result}{e.createdAt ? ` · ${e.createdAt.slice(0, 19).replace("T", " ")}Z` : ""}</div>
              ))}
            </div>
          )}
        </div>

        {/* FREEZE */}
        <div className="beat">
          <div className="bh"><span className="bnum">BEAT 02</span><h3>Emergency freeze</h3>{beats.freeze?.afterFreezeDenied && <span className="denied-pill">403 AFTER</span>}</div>
          <p>CAW has no native freeze API, so freeze = <b style={{ color: "var(--ink)", fontWeight: 600 }}>revoke_pact</b>. A call succeeds, the Pact is revoked, and the very next call is denied - authority stripped instantly.</p>
          {beats.freeze && (
            <>
              <div className="codeline">allowed before · <a className="lnk" href={txUrl(beats.freeze.allowedTx)} target="_blank" rel="noreferrer">{shortHex(beats.freeze.allowedTx, 10)}</a><br />revoke_pact · {shortHex(beats.freeze.pactId, 8)}</div>
              <div className="audit">{beats.freeze.recentDenied.slice(0, 3).map((e, i) => (<div className="ae" key={i}><span className="x">✕</span> {e.action} · {e.result}</div>))}</div>
            </>
          )}
        </div>

        {/* REVIEW */}
        <div className="beat">
          <div className="bh"><span className="bnum">BEAT 03</span><h3>Human-in-the-loop review</h3>{beats.review && <span className="denied-pill ok-pill">APPROVED</span>}</div>
          <p>A <code>review_if</code> Pact holds a sensitive transfer as <b style={{ color: "var(--ink)", fontWeight: 600 }}>PendingApproval</b> until the owner approves - then it executes on-chain.</p>
          {beats.review && (
            <>
              <div className="codeline">decision · <span className="c" style={{ color: "var(--work)" }}>{beats.review.effect}</span><br />pending · {shortHex(beats.review.pendingId, 8)} → {beats.review.status}</div>
              {beats.review.txHash && <div className="audit"><div className="ae"><span style={{ color: "var(--settled)" }}>✓</span> executed · <a className="lnk" href={txUrl(beats.review.txHash)} target="_blank" rel="noreferrer">{shortHex(beats.review.txHash, 10)}</a></div></div>}
            </>
          )}
        </div>
      </div>

      <div className="head" style={{ paddingTop: 8 }}>
        <h1 style={{ fontSize: 22 }}>Pact policies - the literal risk boundary</h1>
        <p>The exact JSON each agent operates within, shipped as a first-class deliverable. The allowlist binds the live v2 escrow ({shortHex(CFG.escrowV2)}) + MockUSDC; caps + review thresholds are enforced by CAW, not by our code.</p>
      </div>
      <div className="pacts">
        {pacts.map((p) => (
          <div className="pact" key={p.name}>
            <div className="ph"><span className="pn">{p.name}</span><span className="pt">Pact spec</span></div>
            <pre>{p.json}</pre>
          </div>
        ))}
      </div>
    </>
  );
}
