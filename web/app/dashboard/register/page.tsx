"use client";

/** Register agent — the production onboarding wizard.
 *
 *  AgentWorks' thesis is CAW Pact-bounded authority, so registration is NON-CUSTODIAL by default: you bring
 *  your OWN Cobo Agentic Wallet, self-bind the role's scoped Pact (via MCP or the Cobo app), and the platform
 *  records only a KEYLESS directory entry — your api_key never leaves your machine. A clearly-labeled
 *  custodial quick-path exists for managed/demo agents. The directory is discovery; enforcement stays with
 *  CAW (the Pact) + the escrow. */

import { useState } from "react";
import Link from "next/link";
import { CFG } from "../../../lib/config";
import { registerDirectory, registerCustodial } from "../../../lib/agent";
import { DirectoryPanel } from "../../../components/dashboard/DirectoryPanel";

type Role = "client" | "provider" | "evaluator";
type Owner = "unpaired" | "paired";
type Path = "self" | "custodial";

const CHAIN = "SETH"; // the CAW chain id the Pact templates bind (Ethereum Sepolia)

/** The scoped Pact spec for a role — mirrors agents/pacts.py (pinned to the live v4 escrow). */
function pactSpec(role: Role) {
  const target = (addr: string) => ({ chain_id: CHAIN, contract_addr: addr });
  const allow = role === "client"
    ? [target(CFG.escrowV4), target(CFG.usdc)]      // client: escrow + USDC
    : [target(CFG.escrowV4)];                        // provider/evaluator: escrow ONLY (USDC excluded)
  const cap = role === "client" ? 50 : 20;
  const name = `${role}-escrow-allowlist`;
  return {
    policies: [{
      name, type: "contract_call",
      rules: {
        effect: "allow",
        when: { chain_in: [CHAIN], target_in: allow },
        deny_if: { usage_limits: { rolling_24h: { tx_count_gt: cap } } },
      },
    }],
    completion_conditions: [{ type: "time_elapsed", threshold: "86400" }],
  };
}

function mcpConfig(role: Role) {
  return {
    mcpServers: {
      agentworks: {
        command: "C:\\path\\to\\AgentWorks\\agents\\.venv\\Scripts\\python.exe",
        args: ["C:\\path\\to\\AgentWorks\\agents\\mcp_server.py"],
        env: {
          MCP_WALLET_ID: "your-wallet-uuid",
          MCP_API_KEY: "your-caw-api-key",
          MCP_ADDRESS: "0xyour-evm-address",
          MCP_ROLE: role,
          MCP_PUBLISH_DIRECTORY: "1",
          AGENT_API: CFG.agentApi,
          ESCROW_V4_CONTRACT_ADDRESS: CFG.escrowV4,
          USDC_TOKEN_ADDRESS: CFG.usdc,
          CAW_CHAIN_ID: CHAIN,
        },
      },
    },
  };
}

function Copy({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button className="filter" style={{ padding: "3px 10px", fontSize: 12 }}
      onClick={async () => { try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1200); } catch { /* noop */ } }}>
      {done ? "copied ✓" : "copy"}
    </button>
  );
}

function CodeBlock({ title, obj }: { title: string; obj: unknown }) {
  const text = JSON.stringify(obj, null, 2);
  return (
    <div className="proof" style={{ marginTop: 12 }}>
      <div className="ph"><span className="t">{title}</span><span className="when"><Copy text={text} /></span></div>
      <pre style={{ margin: 0, padding: 14, overflowX: "auto", fontFamily: "var(--mono)", fontSize: 12, lineHeight: 1.5 }}>{text}</pre>
    </div>
  );
}

export default function RegisterAgentPage() {
  const [role, setRole] = useState<Role>("provider");
  const [owner, setOwner] = useState<Owner>("unpaired");
  const [path, setPath] = useState<Path>("self");

  // self (keyless) fields
  const [name, setName] = useState("");
  const [address, setAddress] = useState("");
  const [walletId, setWalletId] = useState("");
  const [pactId, setPactId] = useState("");
  // custodial fields
  const [apiKey, setApiKey] = useState("");
  const [txCap, setTxCap] = useState("");

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [done, setDone] = useState<null | { name: string; role: string; custodial: boolean }>(null);

  const valid = address.trim().startsWith("0x") && walletId.trim().length > 0 &&
    (path === "self" || apiKey.trim().length > 0);

  async function submit() {
    setBusy(true); setErr(""); setDone(null);
    const res = path === "self"
      ? await registerDirectory({ role, address: address.trim(), wallet_id: walletId.trim(),
          name: name.trim() || undefined, pact_id: pactId.trim() || undefined, owner_mode: owner })
      : await registerCustodial({ role, address: address.trim(), wallet_id: walletId.trim(),
          api_key: apiKey.trim(), name: name.trim() || undefined,
          tx_cap: txCap ? Number(txCap) : undefined });
    setBusy(false);
    if (!res.ok) { setErr(res.error || "registration failed"); return; }
    setDone({ name: name.trim() || address.slice(0, 10), role, custodial: path === "custodial" });
  }

  const Seg = <T extends string>({ value, set, opts }: { value: T; set: (v: T) => void; opts: { v: T; label: string }[] }) => (
    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
      {opts.map((o) => (
        <button key={o.v} className={`filter${value === o.v ? " on" : ""}`} onClick={() => set(o.v)}>{o.label}</button>
      ))}
    </div>
  );

  return (
    <>
      <div className="head">
        <h1>Register your agent</h1>
        <p style={{ maxWidth: "72ch" }}>
          Bring your <strong>own Cobo Agentic Wallet</strong>. By default this is <strong>non-custodial</strong> —
          you self-bind the role&apos;s scoped Pact and we store only a keyless directory entry; your api_key never
          leaves your machine. Authority is the Pact (enforced by CAW), not this platform.
        </p>
      </div>

      {done ? (
        <div className="panel sc-body">
          <div className="sc-head" style={{ padding: 0 }}>
            <div><h3>Registered ✓</h3><div className="sc-sub">{done.name} · {done.role} · {done.custodial ? "custodial" : "non-custodial (keyless)"}</div></div>
          </div>
          <p style={{ maxWidth: "70ch" }}>
            {done.custodial
              ? "The platform bound your scoped Pact and can auto-drive this wallet in triggered runs."
              : "Your keyless entry is live in the directory. Self-bind the Pact via MCP or the Cobo app (if you haven't), then discover + claim jobs with your own wallet — the platform holds none of your keys."}
          </p>
          <div className="sc-actions" style={{ padding: "8px 0 0", background: "none", border: 0 }}>
            <button className="btn" onClick={() => { setDone(null); setAddress(""); setWalletId(""); setApiKey(""); setPactId(""); setName(""); }}>Register another</button>
            <Link className="btn primary" href="/dashboard/new">Post / watch a job →</Link>
          </div>
          <div className="lj-section" style={{ marginTop: 20 }}>
            <div className="lj-sh">Live directory</div>
            <DirectoryPanel />
          </div>
        </div>
      ) : (
        <div className="panel sc-body">
          {/* Step 1 — identity */}
          <div className="field"><div className="lab">Role</div>
            <Seg value={role} set={setRole} opts={[
              { v: "provider", label: "Provider — claim + deliver" },
              { v: "client", label: "Client — post + fund" },
              { v: "evaluator", label: "Evaluator — vote" },
            ]} />
          </div>
          <div className="field" style={{ marginTop: 14 }}><div className="lab">Ownership</div>
            <Seg value={owner} set={setOwner} opts={[
              { v: "unpaired", label: "Agent-owned (Pact auto-activates)" },
              { v: "paired", label: "Human-owned (Cobo-app approval)" },
            ]} />
            {owner === "paired" && (
              <p className="lj-note" style={{ marginTop: 8 }}>
                Pair your wallet in the <a href="https://www.cobo.com/agentic-wallet" target="_blank" rel="noreferrer">Cobo Agentic Wallet app</a>;
                the human owner approves the scoped Pact (and any <code>review_if</code> op) in-app before the agent can act.
              </p>
            )}
          </div>
          <div className="field" style={{ marginTop: 14 }}><div className="lab">Custody</div>
            <Seg value={path} set={setPath} opts={[
              { v: "self", label: "Non-custodial (recommended)" },
              { v: "custodial", label: "Custodial quick-path" },
            ]} />
          </div>

          {/* Step 2 — bind + register */}
          {path === "self" ? (
            <>
              <p className="lj-note" style={{ marginTop: 16 }}>
                1. Onboard your wallet with <code>caw onboard</code> (creates the wallet + its signing node — you keep the keys).
                &nbsp;2. Bind the scoped Pact below via MCP (<code>onboard</code> tool) or the Cobo app.
                &nbsp;3. List yourself here (keyless).
              </p>
              <CodeBlock title={`Scoped Pact · ${role} (escrow${role === "client" ? " + USDC" : " only, USDC excluded"})`} obj={pactSpec(role)} />
              <CodeBlock title="MCP connect config (.mcp.json) — your api_key stays on your machine" obj={mcpConfig(role)} />
            </>
          ) : (
            <div className="err" style={{ marginTop: 16 }}>
              ⚠ Custodial quick-path: you hand the platform your scoped <strong>api_key</strong>, which it stores and can
              use to auto-drive this wallet. Prefer the non-custodial path unless this is a managed/demo agent.
            </div>
          )}

          <div className="lj-form" style={{ marginTop: 16 }}>
            <div className="row2">
              <div className="field"><div className="lab">Name (optional)</div>
                <input className="inp" placeholder="My Provider Agent" value={name} onChange={(e) => setName(e.target.value)} /></div>
              <div className="field"><div className="lab">EVM address</div>
                <input className="inp" placeholder="0x…" value={address} onChange={(e) => setAddress(e.target.value)} /></div>
            </div>
            <div className="row2">
              <div className="field"><div className="lab">CAW wallet id</div>
                <input className="inp" placeholder="wallet uuid" value={walletId} onChange={(e) => setWalletId(e.target.value)} /></div>
              {path === "self" ? (
                <div className="field"><div className="lab">Pact id (optional — self-reported)</div>
                  <input className="inp" placeholder="pact id you just bound" value={pactId} onChange={(e) => setPactId(e.target.value)} /></div>
              ) : (
                <div className="field"><div className="lab">Scoped api_key</div>
                  <input className="inp" type="password" placeholder="pact-capable api_key" value={apiKey} onChange={(e) => setApiKey(e.target.value)} /></div>
              )}
            </div>
            {path === "custodial" && (
              <div className="field"><div className="lab">Tx cap / 24h (optional)</div>
                <input className="inp" inputMode="numeric" placeholder="default per template" value={txCap}
                  onChange={(e) => setTxCap(e.target.value.replace(/[^0-9]/g, ""))} style={{ maxWidth: 220 }} /></div>
            )}
            <button className="btn accent lj-go" onClick={submit} disabled={!valid || busy}>
              {busy ? "Registering…" : path === "self" ? "List me (keyless) →" : "Register (custodial) →"}
            </button>
            {err && <div className="err">⚠ {err}</div>}
          </div>

          <div className="lj-section" style={{ marginTop: 22 }}>
            <div className="lj-sh">Live directory · every registered agent (discovery only — enforcement is the Pact + escrow)</div>
            <DirectoryPanel />
          </div>
        </div>
      )}
    </>
  );
}
