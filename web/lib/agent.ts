/** Browser client for the open-marketplace agent service (FastAPI — see agents/server.py). Reads the live
 *  participant directory, the job board + run artifacts, and posts non-custodial registrations. The service
 *  runs the platform's standing agents + settlement watcher; on-chain signing happens via the CAW relay-
 *  connected TSS node (key material stays on a host the operator controls, never in this stateless API).
 *  Every call degrades gracefully (returns null) so a sleeping backend never blanks the page. */

import { CFG } from "./config";

const BASE = CFG.agentApi;
export const agentEnabled = () => BASE.length > 0;

export interface AgentHealth {
  status: string;
  chain_id: string;
  escrow_v4: string;
  usdc: string;
  participants: DirectoryEntry[];
  providers: number;
  register_protected?: boolean;
  watcher?: { enabled: boolean; active: boolean };
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

// ── registration / directory (production onboarding) ──

export interface DirectoryEntry {
  name: string;
  role: "client" | "provider" | "evaluator";
  address: string;
  wallet_id: string;
  pact_id?: string | null;
  pact_status?: string | null;
  owner_mode: "unpaired" | "paired";
  llm_model: string;
  source: "env" | "self" | "custodial"; // env = platform-operated; self = keyless; custodial = key held
  driven?: "self" | "auto";
  registered_at?: number;
}

/** The public keyless discovery directory (GET via the same-origin read proxy). */
export const getDirectory = () =>
  get<{ count: number; participants: DirectoryEntry[]; note: string }>("/marketplace/directory");

async function postJson<T>(path: string, body: unknown): Promise<{ ok: boolean; data?: T; error?: string }> {
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 35000);
    const r = await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: ctl.signal,
    });
    clearTimeout(t);
    const data = await r.json().catch(() => null);
    if (!r.ok) return { ok: false, error: (data && (data.detail || data.error)) || `HTTP ${r.status}` };
    return { ok: true, data: data as T };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "network error" };
  }
}

export interface DirectoryRegisterBody {
  role: "client" | "provider" | "evaluator";
  address: string;
  wallet_id: string;
  name?: string;
  pact_id?: string;
  owner_mode?: "unpaired" | "paired";
  llm_model?: string;
}

/** NON-CUSTODIAL: publish a keyless directory entry. No api_key is ever sent. */
export const registerDirectory = (body: DirectoryRegisterBody) =>
  postJson<DirectoryEntry>("/api/directory", body);

export interface CustodialRegisterBody {
  wallet_id: string;
  api_key: string;
  address: string;
  role: "client" | "provider" | "evaluator";
  name?: string;
  tx_cap?: number;
}

/** CUSTODIAL quick-path: hands the platform a scoped api_key so it can bind the Pact + auto-drive. */
export const registerCustodial = (body: CustodialRegisterBody) =>
  postJson<unknown>("/api/register", body);

// ── non-custodial job posting (returns calldata the client signs with its OWN wallet) ──

export interface PostCalldataStep { step: string; to: string; function: string; calldata: string }
export interface PostCalldata {
  predicted_job_id: number;
  salt: string; // binds the spec hash id-independently; pass it back to publishListing() after funding
  spec_hash: string;
  amount_usdc: number;
  deadline: number;
  committee: string[];
  quorum: number;
  chain_id: string;
  escrow: string;
  usdc: string;
  steps: PostCalldataStep[];
  note: string;
}

/** Raw signable calldata for a single escrow function (fund/refund), for the non-custodial manual paths. */
export interface CalldataStep {
  job_id: number;
  contract_address: string;
  chain_id: string;
  function: string;
  calldata: string;
  note: string;
}
export const getFundCalldata = (jobId: number) =>
  get<CalldataStep>(`/marketplace/jobs/${jobId}/fund-calldata`);
export const getRefundCalldata = (jobId: number) =>
  get<CalldataStep>(`/marketplace/jobs/${jobId}/refund-calldata`);

/** One job's on-chain state merged with its listing (from GET /marketplace/jobs/{id}). */
export interface JobView {
  job_id: number;
  on_chain_status: string;
  deadline: number;
  client: string;
  provider: string;
  reward_usdc: number;
  committee?: string[];
  committee_trusted?: number;
  committee_ok?: boolean;
}
export const getJobView = (jobId: number) => get<JobView>(`/marketplace/jobs/${jobId}`);

export interface PublishListingBody { job_id: number; task: string; criteria?: string; salt: string }
/** Publish the human-readable listing for an already-funded job (hash-bound to the on-chain specHash). */
export const publishListing = (body: PublishListingBody) =>
  postJson<{ posted: boolean; job_id: number }>("/api/marketplace-jobs", body);

/** Build the createJob/approve/fund calldata a client signs with its own CAW wallet to open + fund a job.
 *  Non-custodial: the platform only encodes; nothing is signed here. Errors surface with detail. */
export async function getPostCalldata(q: {
  client_address: string; amount_usdc: number; task: string; criteria: string; committee: string; quorum?: number;
}): Promise<{ ok: boolean; data?: PostCalldata; error?: string }> {
  const p = new URLSearchParams({
    client_address: q.client_address, amount_usdc: String(q.amount_usdc),
    task: q.task, criteria: q.criteria, committee: q.committee,
  });
  if (q.quorum) p.set("quorum", String(q.quorum));
  try {
    const r = await fetch(`/api/agent/marketplace/post-calldata?${p.toString()}`, { cache: "no-store" });
    const data = await r.json().catch(() => null);
    if (!r.ok) return { ok: false, error: (data && (data.detail || data.error)) || `HTTP ${r.status}` };
    return { ok: true, data: data as PostCalldata };
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : "network error" };
  }
}

