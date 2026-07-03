/** Browser client for the deployed autonomous agent service (FastAPI, Railway - see agents/server.py).
 *  The dashboard is a LIVE WINDOW onto these agents: "Post job" fires POST /trigger and the board polls
 *  /runs + /health to watch them reason, race to accept, deliver, and settle. The service runs the
 *  autonomous orchestration + LLM reasoning in the cloud; on-chain signing happens via the CAW relay-
 *  connected TSS node (key material stays on a host the operator controls, never in this stateless API).
 *  Every call degrades gracefully (returns null) so a sleeping backend never blanks the page. */

import { CFG } from "./config";

const BASE = CFG.agentApi;
export const agentEnabled = () => BASE.length > 0;

export interface Participant {
  name: string;
  role: "client" | "provider" | string;
  wallet_id: string;
  address: string;
}

export interface AgentHealth {
  status: string;
  chain_id: string;
  escrow_v2: string;
  usdc: string;
  participants: Participant[];
  providers: number;
  run: { active: boolean; run_id: string | null; mode: string | null; started_at: number | null };
  trigger_protected: boolean;
  register_protected?: boolean;
}

/** One run artifact as written by agents/autonomous.py (Run.write_artifact). */
export interface AgentRun {
  run_id: string;
  job_id: number;
  txs: Record<string, string>;
  accept_decisions: Record<string, { accept: boolean; reason: string }>;
  winner: string | null;
  winner_addr: string | null;
  irys: { id: string; url: string; bytes?: number } | null;
  deliverable: string | null;
  verdict: { accept: boolean; reason: string } | null;
  branch: "payout" | "refund" | null;
  status: string;
  // v4 committee + dispute fields
  committee?: string[];
  committee_votes?: Record<string, { addr: string; accept: boolean; reason: string }>;
  vote_txs?: Record<string, string>;
  tentative?: "payout" | "refund" | null;
  quorum?: number;
  task?: string;
  criteria?: string;
  amount_usdc?: number;
  client?: string;
  provider?: string;
  fund_decision?: { fund: boolean; reason: string };
  final_status?: string;
  content_verified?: boolean | null;
}

export interface BoardListing {
  job_id: number;
  task: string;
  criteria: string;
  reward_usdc: number;
  spec_hash: string;
  client: string;
  deadline: number;
  posted_at: number;
}

async function get<T>(path: string, timeoutMs = 8000): Promise<T | null> {
  if (!BASE) return null;
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), timeoutMs);
    // Go through the same-origin proxy (/api/agent/*) so an HTTPS dashboard can read a plain-HTTP agent
    // service without mixed-content blocking. These readers run in client components.
    const r = await fetch(`/api/agent${path}`, { signal: ctl.signal, cache: "no-store" });
    clearTimeout(t);
    if (!r.ok) return null;
    return (await r.json()) as T;
  } catch {
    return null;
  }
}

export const getHealth = () => get<AgentHealth>("/health", 12000);
export const getRuns = () => get<AgentRun[]>("/runs");
export const getBoard = () => get<Record<string, BoardListing>>("/board");

export interface TriggerBody {
  task?: string;
  criteria?: string;
  mode?: "good" | "bad";
  reward_usdc?: number;
  max_jobs?: number;
}
export interface TriggerResult {
  accepted: boolean;
  mode: string;
  reward_usdc: number;
  max_jobs: number;
  poll: string;
}

/** Launch an autonomous run. Returns {ok,data} on success, else {ok:false,error} (e.g. 409 run-active).
 *  Posts to the SAME-ORIGIN /api/trigger route (not the agent service directly): the agent service's
 *  /trigger is bearer-token protected and that token lives only on the server (web/app/api/trigger/route.ts),
 *  never in the browser. */
export async function trigger(body: TriggerBody): Promise<{ ok: boolean; data?: TriggerResult; error?: string }> {
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 15000);
    const r = await fetch(`/api/trigger`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ mode: "good", reward_usdc: 5, max_jobs: 1, ...body }),
      signal: ctl.signal,
    });
    clearTimeout(t);
    if (!r.ok) {
      const detail = await r.json().catch(() => null);
      return { ok: false, error: detail?.detail ?? `HTTP ${r.status}` };
    }
    return { ok: true, data: (await r.json()) as TriggerResult };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "network error" };
  }
}
