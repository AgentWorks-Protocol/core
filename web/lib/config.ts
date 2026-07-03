/** Public testnet config surfaced by the dashboard. No secrets - all values are public
 *  (deployed contracts, public RPC, explorer). Defaults mirror docs/FACTS.md + docs/STATUS.md. */

const env = (k: string, d: string) => (process.env[k] && process.env[k]!.length > 0 ? process.env[k]! : d);

export const CFG = {
  chainId: 11155111,
  rpc: env("NEXT_PUBLIC_RPC_URL", "https://sepolia.drpc.org"),
  escrow: env("NEXT_PUBLIC_ESCROW_ADDRESS", "0x812BcEEc2De8C8aC71C7af7A8E2d4467E65Fdf18") as `0x${string}`,
  // v2 open-marketplace escrow (Phase 6.5, legacy): createJob without provider + raw acceptJob(jobId).
  escrowV2: env("NEXT_PUBLIC_ESCROW_V2_ADDRESS", "0xD6cB413c0E4a5839Fd4B02aFFeBF65e6868726b9") as `0x${string}`,
  // v3 open-marketplace escrow (MEV-hardened): sealed commit-reveal accept. Superseded by v4.
  escrowV3: env("NEXT_PUBLIC_ESCROW_V3_ADDRESS", "0xFAab4d6ff5CBEcD72a4e1B9315662e7846166D69") as `0x${string}`,
  // v4 open-marketplace escrow: committee (M-of-N) consensus evaluation + staked disputes escalating to a
  // decoupled, decentralized arbiter (UMA OOv3). This is the LIVE marketplace the dashboard reads.
  escrowV4: env("NEXT_PUBLIC_ESCROW_V4_ADDRESS", "0x8F60e34e43Dd53Bd170633fB5b1d8c43e21C264C") as `0x${string}`,
  // Previous v4 deployment (identical bytecode, superseded only to widen the voting window). The dashboard
  // also reads it so historical jobs that settled there still resolve their status + settling tx.
  escrowV4Prev: env("NEXT_PUBLIC_ESCROW_V4_PREV_ADDRESS", "0x86B422CC8F75B7c5521a2552F2C34da8cb342C86") as `0x${string}`,
  umaArbiter: env("NEXT_PUBLIC_UMA_ARBITER", "0x8BDB79EB6cDC3E54E373C0E5096CffD737a5DE4B") as `0x${string}`,
  // Deployed autonomous agent service (DigitalOcean droplet, co-located with the TSS signer). The dashboard
  // is a live window onto these agents: "Post job" fires POST /trigger and the board watches them reason +
  // settle. Reads proxy through /api/agent/* (same-origin) so an HTTPS page can reach the HTTP service.
  // Empty → live-trigger UI hidden. Override per-env with NEXT_PUBLIC_AGENT_API.
  agentApi: env("NEXT_PUBLIC_AGENT_API", "http://139.59.135.74:8000").replace(/\/$/, ""),
  usdc: env("NEXT_PUBLIC_USDC_ADDRESS", "0x4C4D1223BcC47E380CF4C37652EaDFe10A9Fd910") as `0x${string}`,
  clientCaw: env("NEXT_PUBLIC_CLIENT_CAW", "0x6dfbd0ac9feb5bb9a9ffeaf54df203c1633c1ddd") as `0x${string}`,
  providerCaw: env("NEXT_PUBLIC_PROVIDER_CAW", "0xef9349b3273b1a54faaf701231f499fe0282e643") as `0x${string}`,
  // 2nd provider address (same Provider wallet, distinct msg.sender) - the live accept-race competitor.
  providerCawB: env("NEXT_PUBLIC_PROVIDER_CAW_B", "0x7ea0701d657e3427c2bb3bc195e943a81c5fc69e") as `0x${string}`,
  explorer: env("NEXT_PUBLIC_EXPLORER_BASE", "https://sepolia.etherscan.io"),
  irysGateway: env("NEXT_PUBLIC_IRYS_GATEWAY", "https://devnet.irys.xyz"),
  enableLiveRun: env("NEXT_PUBLIC_ENABLE_LIVE_RUN", "1") !== "0",
};

export const txUrl = (h: string) => `${CFG.explorer}/tx/${h}`;
export const addrUrl = (a: string) => `${CFG.explorer}/address/${a}`;
export const irysUrl = (id: string) => `${CFG.irysGateway}/${id}`;

/** 0x1234…cdef - short form for addresses/hashes (mono in the UI). */
export const shortHex = (s: string, head = 6, tail = 4) =>
  !s ? "" : s.length > head + tail + 1 ? `${s.slice(0, head)}…${s.slice(-tail)}` : s;
