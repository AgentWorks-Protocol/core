/** Server-side proxy for POST /marketplace/register — the CUSTODIAL quick-path.
 *
 *  This is the ONLY route that ever receives an api_key from the browser. The platform will hold that
 *  scoped api_key and can auto-drive the wallet, so this path is custodial by design (the trustless path is
 *  /api/directory + self-driving via MCP). Gate it with AGENT_REGISTER_TOKEN for a curated pool; the token
 *  is attached server-side and never reaches the browser.
 *
 *  Env (server-only, NOT NEXT_PUBLIC):
 *    AGENT_REGISTER_TOKEN  - bearer token the agent service requires on registration (optional).
 */

import { NextRequest, NextResponse } from "next/server";
import { CFG } from "../../../lib/config";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BASE = CFG.agentApi;
const TOKEN = process.env.AGENT_REGISTER_TOKEN || "";

export async function POST(req: NextRequest) {
  if (!BASE) {
    return NextResponse.json({ ok: false, error: "agent service not configured" }, { status: 503 });
  }
  // Custodial registration transmits a CAW api_key. Refuse to send it over a non-TLS link (the default droplet
  // is plain HTTP) — it would travel in cleartext. Auto-enables once the agent service is behind HTTPS. Use the
  // keyless directory path (/api/directory) or register via MCP with your own wallet instead.
  if (!BASE.startsWith("https://")) {
    return NextResponse.json(
      { ok: false, error: "Custodial registration is disabled: it would send your CAW api_key in cleartext over a non-HTTPS agent service. Use keyless directory registration, or register via MCP with your own wallet." },
      { status: 400 },
    );
  }
  let body: unknown = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "invalid JSON body" }, { status: 400 });
  }
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 30000); // custodial register binds a Pact (slower)
    const r = await fetch(`${BASE}/marketplace/register`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(TOKEN ? { authorization: `Bearer ${TOKEN}` } : {}),
      },
      body: JSON.stringify(body ?? {}),
      signal: ctl.signal,
      cache: "no-store",
    });
    clearTimeout(t);
    const data = await r.json().catch(() => null);
    return NextResponse.json(data ?? { ok: r.ok }, { status: r.status });
  } catch (e) {
    return NextResponse.json(
      { ok: false, error: e instanceof Error ? e.message : "network error" },
      { status: 502 },
    );
  }
}
