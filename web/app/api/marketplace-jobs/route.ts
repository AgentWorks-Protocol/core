/** Server-side proxy for POST /marketplace/jobs (publish a funded job's human-readable listing).
 *
 *  Non-custodial: the browser sends only {job_id, task, criteria, salt} — no keys. The agent service
 *  re-derives the salt-bound spec hash and rejects anything that doesn't reproduce the job's on-chain
 *  specHash, so this can't poison the board. Reads use the same-origin GET proxy (/api/agent/...).
 */

import { NextRequest, NextResponse } from "next/server";
import { CFG } from "../../../lib/config";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BASE = CFG.agentApi;

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
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 15000);
    const r = await fetch(`${BASE}/marketplace/jobs`, {
      method: "POST",
      headers: { "content-type": "application/json" },
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
