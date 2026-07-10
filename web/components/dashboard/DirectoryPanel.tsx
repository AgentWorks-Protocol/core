"use client";

/** The LIVE participant directory — every participant in the marketplace, read from the ONE canonical source
 *  GET /marketplace/directory (env platform-operated + custodial + keyless self-registered), cross-checked
 *  against /health. Discovery only: pact_status is self-reported; enforcement is CAW (the Pact) + the escrow. */

import { useEffect, useState } from "react";
import { getDirectory, getHealth, type DirectoryEntry } from "../../lib/agent";
import { addrUrl, shortHex } from "../../lib/config";

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
      setRows([...byAddr.values()]);
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
