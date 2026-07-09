/** Server-side proxy for POST /marketplace/register-directory (the NON-CUSTODIAL, keyless join).
 *
 *  The browser never sends an api_key here — only non-secret identity (address, wallet_id, role, pact_id).
 *  Registration can be gated by AGENT_REGISTER_TOKEN (server-only, curated pool); this route attaches it so
 *  the token never reaches the browser. Directory READS use the same-origin GET proxy (/api/agent/...).
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
  let body: unknown = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "invalid JSON body" }, { status: 400 });
  }
  // Defensive: this route is keyless by contract — never forward an api_key even if one is present.
  if (body && typeof body === "object" && "api_key" in (body as Record<string, unknown>)) {
    delete (body as Record<string, unknown>).api_key;
  }
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 15000);
    const r = await fetch(`${BASE}/marketplace/register-directory`, {
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
