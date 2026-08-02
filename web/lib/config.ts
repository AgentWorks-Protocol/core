/** Public testnet config surfaced by the dashboard. No secrets - all values are public
 *  (deployed contracts, public RPC, explorer). Defaults mirror docs/FACTS.md + docs/STATUS.md. */

const env = (k: string, d: string) => (process.env[k] && process.env[k]!.length > 0 ? process.env[k]! : d);

export const CFG = {
  chainId: 84532,
  rpc: env("NEXT_PUBLIC_RPC_URL", "https://base-sepolia.drpc.org"),
  escrow: env("NEXT_PUBLIC_ESCROW_ADDRESS", "0x812BcEEc2De8C8aC71C7af7A8E2d4467E65Fdf18") as `0x${string}`,
  // v2 open-marketplace escrow (Phase 6.5, legacy): createJob without provider + raw acceptJob(jobId).
  escrowV2: env("NEXT_PUBLIC_ESCROW_V2_ADDRESS", "0xD6cB413c0E4a5839Fd4B02aFFeBF65e6868726b9") as `0x${string}`,
  // v3 open-marketplace escrow (MEV-hardened): sealed commit-reveal accept. Superseded by v4.
  escrowV3: env("NEXT_PUBLIC_ESCROW_V3_ADDRESS", "0xFAab4d6ff5CBEcD72a4e1B9315662e7846166D69") as `0x${string}`,
  // v4 open-marketplace escrow: committee (M-of-N) consensus evaluation + staked disputes escalating to a
  // decoupled, decentralized arbiter (UMA OOv3). This is the LIVE marketplace the dashboard reads.
  escrowV4: env("NEXT_PUBLIC_ESCROW_V4_ADDRESS", "0xDAC780EdD2a1c082b019d12952E3b93599da2A6C") as `0x${string}`,
  // No separate "previous" v4 on Base Sepolia — points at the same live escrow (the V4_ESCROWS merge dedups,
  // so this is a harmless no-op; a Sepolia address here would revert listJobsV2 on the Base client).
  escrowV4Prev: env("NEXT_PUBLIC_ESCROW_V4_PREV_ADDRESS", "0xDAC780EdD2a1c082b019d12952E3b93599da2A6C") as `0x${string}`,
  umaArbiter: env("NEXT_PUBLIC_UMA_ARBITER", "0x6bf5eA821BE4990544B3F5C610C55A97857EcdCb") as `0x${string}`,
  // Open-marketplace agent service (DigitalOcean droplet, co-located with the TSS signer). It runs the
  // platform's standing agents + settlement watcher and serves the marketplace directory + calldata rail.
  // Reads proxy through /api/agent/* (same-origin) so an HTTPS page can reach the HTTP service. Override
  // per-env with NEXT_PUBLIC_AGENT_API.
  agentApi: env("NEXT_PUBLIC_AGENT_API", "http://139.59.135.74:8000").replace(/\/$/, ""),
  usdc: env("NEXT_PUBLIC_USDC_ADDRESS", "0x036CbD53842c5426634e7929541eC2318f3dCF7e") as `0x${string}`,
  // NOTE: no hardcoded platform client/provider wallets here — this is an open market. Job pages show the
  // REAL client/provider from the artifact or the on-chain job tuple; the directory shows registered agents.
  explorer: env("NEXT_PUBLIC_EXPLORER_BASE", "https://sepolia.basescan.org"),
  irysGateway: env("NEXT_PUBLIC_IRYS_GATEWAY", "https://devnet.irys.xyz"),
};

export const txUrl = (h: string) => `${CFG.explorer}/tx/${h}`;
export const addrUrl = (a: string) => `${CFG.explorer}/address/${a}`;
export const irysUrl = (id: string) => `${CFG.irysGateway}/${id}`;

/** 0x1234…cdef - short form for addresses/hashes (mono in the UI). */
export const shortHex = (s: string, head = 6, tail = 4) =>
  !s ? "" : s.length > head + tail + 1 ? `${s.slice(0, head)}…${s.slice(-tail)}` : s;
