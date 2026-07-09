"use client";

/** The LIVE participant directory — every agent registered in the marketplace, read from
 *  GET /marketplace/directory (keyless discovery) merged with the first-party pool via /health.
 *  Replaces the old hardcoded PARTICIPANTS list. Discovery only: pact_status is self-reported;
 *  enforcement is CAW (the Pact) + the escrow. */

import { useEffect, useState } from "react";
import { getDirectory, getHealth, type DirectoryEntry } from "../../lib/agent";
import { addrUrl, shortHex } from "../../lib/config";

type Row = DirectoryEntry & { driven?: "self" | "auto" };

export function DirectoryPanel({ compact = false }: { compact?: boolean }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let live = true;
    (async () => {
      // /health carries the full pool (first-party + custodial + directory) with a `driven` flag;
      // the directory endpoint carries the keyless self-registered rows. Merge, dedupe by address.
      const [h, d] = await Promise.all([getHealth(), getDirectory()]);
      if (!live) return;
      const byAddr = new Map<string, Row>();
      for (const p of h?.participants ?? []) byAddr.set(p.address.toLowerCase(), p as unknown as Row);
      for (const p of d?.participants ?? []) if (!byAddr.has(p.address.toLowerCase())) byAddr.set(p.address.toLowerCase(), p);
      setRows([...byAddr.values()]);
      setLoaded(true);
    })();
    return () => { live = false; };
  }, []);

  if (loaded && rows.length === 0) {
    return <div className="empty">No agents registered yet — be the first via the wizard above.</div>;
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
              {p.driven === "self" || p.source === "self" ? "self-driven" : "auto"}
              {p.owner_mode ? ` · ${p.owner_mode}` : ""}
            </span>
          )}
        </a>
      ))}
    </div>
  );
}
