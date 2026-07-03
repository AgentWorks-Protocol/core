/** Same-origin GET proxy for the agent service's read endpoints (/health, /runs, /runs/{id}, /board).
 *
 *  The dashboard is served over HTTPS but the agent service runs over plain HTTP on the signer droplet, so a
 *  browser-direct fetch would be blocked as mixed content. Client components fetch `/api/agent/<path>` here
 *  instead; this route (server-side) forwards to the agent service and returns the JSON. Reads are public, so
 *  no token is attached (POST /trigger stays on its own token-attaching route).
 */
import { NextRequest, NextResponse } from "next/server";
import { CFG } from "../../../../lib/config";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BASE = CFG.agentApi;

export async function GET(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  if (!BASE) return NextResponse.json(null, { status: 200 });
  const { path } = await ctx.params;
  const target = `${BASE}/${path.join("/")}${req.nextUrl.search}`;
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 12000);
    const r = await fetch(target, { signal: ctl.signal, cache: "no-store" });
    clearTimeout(t);
    const data = await r.json().catch(() => null);
    return NextResponse.json(data, { status: r.status });
  } catch {
    return NextResponse.json(null, { status: 502 });
  }
}
