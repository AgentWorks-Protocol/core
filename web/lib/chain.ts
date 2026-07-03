/** Read-only on-chain enrichment via viem (server-side). Additive: every call is wrapped so a
 *  flaky RPC degrades gracefully to the proof snapshots - the dashboard never goes blank. */

import { createPublicClient, http, formatUnits } from "viem";
import { sepolia } from "viem/chains";
import { CFG } from "./config";
import { escrowAbi, escrowAbiV4, erc20Abi, STATUS_LABELS, STATUS_LABELS_V4, ESCROW_V4_FROM_BLOCK, ESCROW_V4_PREV_FROM_BLOCK } from "./abi";

const client = createPublicClient({
  chain: sepolia,
  transport: http(CFG.rpc, { timeout: 6000 }),
});

async function safe<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch {
    return null;
  }
}

/** USDC (6-decimal MockUSDC) balance as a display number, or null if unreachable. */
export async function usdcBalance(address: `0x${string}`): Promise<number | null> {
  const raw = await safe(
    client.readContract({ address: CFG.usdc, abi: erc20Abi, functionName: "balanceOf", args: [address] }),
  );
  return raw === null ? null : Number(formatUnits(raw as bigint, 6));
}

export interface LiveJob {
  amountUsdc: number;
  statusLabel: string;
  irysId: string;
}

/** Live getJob() for a job id, or null if unreachable / not found. */
export async function liveJob(jobId: number): Promise<LiveJob | null> {
  const j = await safe(
    client.readContract({ address: CFG.escrow, abi: escrowAbi, functionName: "getJob", args: [BigInt(jobId)] }),
  );
  if (!j) return null;
  const job = j as { amount: bigint; irysId: string; status: number };
  return {
    amountUsdc: Number(formatUnits(job.amount, 6)),
    statusLabel: STATUS_LABELS[job.status] ?? "Unknown",
    irysId: job.irysId,
  };
}

/** Current job counter (first id is 1), or null if unreachable. */
export async function nextJobId(): Promise<number | null> {
  const n = await safe(client.readContract({ address: CFG.escrow, abi: escrowAbi, functionName: "nextJobId" }));
  return n === null ? null : Number(n as bigint);
}

import type { BadgeState } from "../components/Badge";
// Solidity Status enum index → lifecycle badge.
const STATUS_BADGE: BadgeState[] = ["open", "open", "escrow", "work", "settled", "reclaim", "reclaim"];

export interface ChainJob {
  jobId: number;
  amountUsdc: number;
  statusLabel: string;
  badge: BadgeState;
  irysId: string;
  client: string;
  provider: string;
}

/** Every job currently on-chain (newest first), so the Marketplace reflects live escrow state.
 *  Returns null if the RPC is unreachable (caller falls back to the artifact snapshot). */
export async function listJobs(max = 40): Promise<ChainJob[] | null> {
  const n = await nextJobId();
  if (n === null) return null;
  const ids: number[] = [];
  for (let i = Math.max(1, n - max); i < n; i++) ids.push(i);
  const rows = await Promise.all(
    ids.map(async (id) => {
      const j = await safe(
        client.readContract({ address: CFG.escrow, abi: escrowAbi, functionName: "getJob", args: [BigInt(id)] }),
      );
      if (!j) return null;
      const job = j as { client: string; provider: string; amount: bigint; irysId: string; status: number };
      return {
        jobId: id,
        amountUsdc: Number(formatUnits(job.amount, 6)),
        statusLabel: STATUS_LABELS[job.status] ?? "Unknown",
        badge: STATUS_BADGE[job.status] ?? "open",
        irysId: job.irysId,
        client: job.client,
        provider: job.provider,
      } as ChainJob;
    }),
  );
  return rows.filter((r): r is ChainJob => r !== null).reverse();
}

// ── open marketplace (the LIVE v4 committee + dispute escrow) ──
// These readers keep the historical "V2" names (callers unchanged) but now target the v4 escrow
// (CFG.escrowV4) with the v4 Job tuple + status enum (adds Resolved + Disputed). The v4 commit-reveal
// accept is carried from v3; settlement is committee consensus → finalize/dispute.
// v4 Status index → badge: None,Open,Funded,Accepted,Submitted,Resolved,Disputed,Completed,Rejected,Refunded.
const STATUS_BADGE_V2: BadgeState[] = ["open", "open", "escrow", "work", "work", "work", "work", "settled", "reclaim", "reclaim"];

/** Current marketplace (v4) job counter, or null if unreachable. */
export async function nextJobIdV2(): Promise<number | null> {
  const n = await safe(client.readContract({ address: CFG.escrowV4, abi: escrowAbiV4, functionName: "nextJobId" }));
  return n === null ? null : Number(n as bigint);
}

export interface LiveJobV2 {
  amountUsdc: number;
  statusLabel: string;
  irysId: string;
  client: string;
  provider: string;
}

export interface SettlementV2 {
  outcome: "payout" | "refund";
  txHash: `0x${string}`;
}

// The two identical-bytecode v4 deployments the dashboard resolves against: the live escrow first, then the
// previous one (widened only for the voting window). Job ids can repeat across them, so we prefer the live
// escrow on a collision (e.g. job #1 = the live committee payout).
const V4_ESCROWS: { address: `0x${string}`; fromBlock: bigint }[] = [
  { address: CFG.escrowV4, fromBlock: ESCROW_V4_FROM_BLOCK },
  { address: CFG.escrowV4Prev, fromBlock: ESCROW_V4_PREV_FROM_BLOCK },
];

/** Recover a job's settlement (which tx settled it + the outcome) from chain events, for jobs that have no
 *  run artifact. Scans both v4 escrows (live first). Best-effort: null if unreachable/unsupported/unsettled. */
export async function settlementV2(jobId: number): Promise<SettlementV2 | null> {
  for (const esc of V4_ESCROWS) {
    const scan = (eventName: "JobCompleted" | "JobRejected" | "RefundClaimed") =>
      safe(
        client.getContractEvents({
          address: esc.address,
          abi: escrowAbiV4,
          eventName,
          args: { jobId: BigInt(jobId) },
          fromBlock: esc.fromBlock,
          toBlock: "latest",
        }),
      );
    const [completed, rejected, refunded] = await Promise.all([scan("JobCompleted"), scan("JobRejected"), scan("RefundClaimed")]);
    const hit = completed?.[0]
      ? { outcome: "payout" as const, log: completed[0] }
      : rejected?.[0]
        ? { outcome: "refund" as const, log: rejected[0] }
        : refunded?.[0]
          ? { outcome: "refund" as const, log: refunded[0] }
          : null;
    if (hit?.log?.transactionHash) return { outcome: hit.outcome, txHash: hit.log.transactionHash };
  }
  return null;
}

/** Live getJob() on the marketplace (v4) escrow, or null if unreachable / not found. Tries the live escrow
 *  first, then the previous v4 deployment (so historical jobs still resolve). */
export async function liveJobV2(jobId: number): Promise<LiveJobV2 | null> {
  for (const esc of V4_ESCROWS) {
    const j = await safe(
      client.readContract({ address: esc.address, abi: escrowAbiV4, functionName: "getJob", args: [BigInt(jobId)] }),
    );
    if (!j) continue; // getJob reverts for unknown ids → try the other escrow
    const job = j as { client: string; provider: string; amount: bigint; irysId: string; status: number };
    return {
      amountUsdc: Number(formatUnits(job.amount, 6)),
      statusLabel: STATUS_LABELS_V4[job.status] ?? "Unknown",
      irysId: job.irysId,
      client: job.client,
      provider: job.provider,
    };
  }
  return null;
}

/** Committee + vote state for a v4 job (for the committee panel). Null if unreachable. */
export interface CommitteeInfo {
  members: string[];
  approveCount: number;
  rejectCount: number;
  quorum: number;
  tentativePayout: boolean;
  resolvedBlock: number;
}
export async function committeeV4(jobId: number): Promise<CommitteeInfo | null> {
  const [members, vote, job] = await Promise.all([
    safe(client.readContract({ address: CFG.escrowV4, abi: escrowAbiV4, functionName: "getCommittee", args: [BigInt(jobId)] })),
    safe(client.readContract({ address: CFG.escrowV4, abi: escrowAbiV4, functionName: "getVote", args: [BigInt(jobId)] })),
    safe(client.readContract({ address: CFG.escrowV4, abi: escrowAbiV4, functionName: "getJob", args: [BigInt(jobId)] })),
  ]);
  if (!members || !vote) return null;
  const v = vote as readonly [number, number, bigint, boolean, bigint];
  return {
    members: members as string[],
    approveCount: Number(v[0]),
    rejectCount: Number(v[1]),
    quorum: job ? Number((job as { quorum: number }).quorum) : 0,
    tentativePayout: v[3],
    resolvedBlock: Number(v[4]),
  };
}

/** Every job on the marketplace (v4) escrow (newest first). Null if the RPC is unreachable. */
export async function listJobsV2(max = 40): Promise<ChainJob[] | null> {
  const n = await nextJobIdV2();
  if (n === null) return null;
  const ids: number[] = [];
  for (let i = Math.max(1, n - max); i < n; i++) ids.push(i);
  const rows = await Promise.all(
    ids.map(async (id) => {
      const j = await safe(
        client.readContract({ address: CFG.escrowV4, abi: escrowAbiV4, functionName: "getJob", args: [BigInt(id)] }),
      );
      if (!j) return null;
      const job = j as { client: string; provider: string; amount: bigint; irysId: string; status: number };
      return {
        jobId: id,
        amountUsdc: Number(formatUnits(job.amount, 6)),
        statusLabel: STATUS_LABELS_V4[job.status] ?? "Unknown",
        badge: STATUS_BADGE_V2[job.status] ?? "open",
        irysId: job.irysId,
        client: job.client,
        provider: job.provider,
      } as ChainJob;
    }),
  );
  return rows.filter((r): r is ChainJob => r !== null).reverse();
}
