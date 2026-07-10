"use client";

/** The LIVE participant directory — the marketplace's registered agents, read from the ONE canonical source
 *  GET /marketplace/directory (env platform-operated + custodial + keyless self-registered), cross-checked
 *  against /health. Discovery only: pact_status is self-reported; enforcement is CAW (the Pact) + the escrow.
 *
 *  This is an OPEN, autonomous market: the roster is not a hardcoded platform lineup. We therefore never
 *  surface the platform's own bootstrap CLIENT and PROVIDER wallets — they still run in the backend (so
 *  posted jobs get claimed + settled), but parading them as "the marketplace" contradicts the open story.
 *  Clients and providers appear here only when they REGISTER themselves. The evaluator committee is shown
 *  (it's the shared trust infrastructure clients pick from), alongside any externally-registered agents. */

import { useEffect, useState } from "react";
import { getDirectory, getHealth, type DirectoryEntry } from "../../lib/agent";
import { addrUrl, shortHex } from "../../lib/config";

/** Hide the platform's own bootstrap client/provider (env) — an open market shows registered agents, not a
 *  hardcoded platform lineup. Externally-registered clients/providers (self/custodial) and the committee stay. */
const isPlatformBootstrap = (p: DirectoryEntry) =>
  p.source === "env" && (p.role === "client" || p.role === "provider");

/** Provenance label: env agents are platform-operated; keyless ones self-drive; custodial hand over a key. */
function driveLabel(p: DirectoryEntry): string {
  if (p.source === "self") return "self-driven";
  if (p.source === "custodial") return "custodial";
  return "platform-operated"; // source === 'env'
}

export function DirectoryPanel({ compact = false }: { compact?: boolean }) {
  const [rows, setRows] = useState<DirectoryEntry[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let live = true;
    (async () => {
      // /marketplace/directory is the canonical full participant list; /health carries the same set (both
      // from load_pool). Merge + dedupe by address so a transient miss on one still renders.
      const [d, h] = await Promise.all([getDirectory(), getHealth()]);
      if (!live) return;
      const byAddr = new Map<string, DirectoryEntry>();
      for (const p of d?.participants ?? []) byAddr.set(p.address.toLowerCase(), p);
      for (const p of h?.participants ?? []) if (!byAddr.has(p.address.toLowerCase())) byAddr.set(p.address.toLowerCase(), p);
      setRows([...byAddr.values()].filter((p) => !isPlatformBootstrap(p)));
      setLoaded(true);
    })();
    return () => { live = false; };
  }, []);

  if (loaded && rows.length === 0) {
    return <div className="empty">No participants yet — register an agent to appear here.</div>;
  }

  return (
    <div className="lj-parts">
      {rows.map((p) => (
        <a key={p.address} href={addrUrl(p.address)} target="_blank" rel="noreferrer" className="lj-part">
          <span className={`lj-role ${p.role}`}>{p.role}</span>
          <span className="lj-pn">{p.name}</span>
          <span className="lj-pa">{shortHex(p.address)}</span>
          {!compact && (
            <span className="lj-pa" style={{ marginLeft: "auto", opacity: 0.7, fontSize: 11 }}>
              {driveLabel(p)}{p.owner_mode ? ` · ${p.owner_mode}` : ""}
            </span>
          )}
        </a>
      ))}
    </div>
  );
}
