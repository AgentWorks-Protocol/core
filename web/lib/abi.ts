/** Minimal ABIs for read-only dashboard enrichment. Mirrors contracts/src/AgentWorksEscrow.sol. */

export const escrowAbi = [
  {
    type: "function",
    name: "getJob",
    stateMutability: "view",
    inputs: [{ name: "jobId", type: "uint256" }],
    outputs: [
      {
        name: "",
        type: "tuple",
        components: [
          { name: "client", type: "address" },
          { name: "provider", type: "address" },
          { name: "evaluator", type: "address" },
          { name: "amount", type: "uint256" },
          { name: "specHash", type: "bytes32" },
          { name: "deliverableHash", type: "bytes32" },
          { name: "irysId", type: "string" },
          { name: "deadline", type: "uint64" },
          { name: "status", type: "uint8" },
        ],
      },
    ],
  },
  { type: "function", name: "nextJobId", stateMutability: "view", inputs: [], outputs: [{ type: "uint256" }] },
  // settlement events (jobId indexed) — let the dashboard recover the settle tx + outcome from chain
  // for jobs that have no run artifact (e.g. MCP-settled jobs).
  {
    type: "event", name: "JobCompleted",
    inputs: [
      { name: "jobId", type: "uint256", indexed: true },
      { name: "provider", type: "address", indexed: true },
      { name: "amount", type: "uint256", indexed: false },
    ],
  },
  {
    type: "event", name: "JobRejected",
    inputs: [
      { name: "jobId", type: "uint256", indexed: true },
      { name: "client", type: "address", indexed: true },
      { name: "amount", type: "uint256", indexed: false },
    ],
  },
  {
    type: "event", name: "RefundClaimed",
    inputs: [
      { name: "jobId", type: "uint256", indexed: true },
      { name: "client", type: "address", indexed: true },
      { name: "amount", type: "uint256", indexed: false },
    ],
  },
  // v3 sealed-accept commit phase: opaque hash only (no jobId) — lets the dashboard show "sealed bids".
  {
    type: "event", name: "AcceptCommitted",
    inputs: [
      { name: "commitment", type: "bytes32", indexed: true },
      { name: "commitBlock", type: "uint64", indexed: false },
    ],
  },
] as const;

/** Block the v2 open-marketplace escrow was deployed at — lower bound for settlement-event log scans. */
export const ESCROW_V2_FROM_BLOCK = 11035232n;
/** Block the v3 (commit-reveal) escrow was deployed at — lower bound for v3 event log scans. */
export const ESCROW_V3_FROM_BLOCK = 11087195n;

export const erc20Abi = [
  { type: "function", name: "balanceOf", stateMutability: "view", inputs: [{ name: "a", type: "address" }], outputs: [{ type: "uint256" }] },
  { type: "function", name: "decimals", stateMutability: "view", inputs: [], outputs: [{ type: "uint8" }] },
] as const;

/** v1 Status enum index → human label (matches AgentWorksEscrow.sol). */
export const STATUS_LABELS = ["None", "Created", "Funded", "Submitted", "Completed", "Rejected", "Refunded"] as const;

/** v2 Status enum index → human label (matches AgentWorksEscrowV2.sol - adds Open + Accepted). */
export const STATUS_LABELS_V2 = ["None", "Open", "Funded", "Accepted", "Submitted", "Completed", "Rejected", "Refunded"] as const;

/** v4 Status enum index → human label (adds Resolved + Disputed; Completed shifts to 7). */
export const STATUS_LABELS_V4 = ["None", "Open", "Funded", "Accepted", "Submitted", "Resolved", "Disputed", "Completed", "Rejected", "Refunded"] as const;

/** Block the v4 (committee + disputes) escrow was deployed at — lower bound for v4 event log scans. */
export const ESCROW_V4_FROM_BLOCK = 11189574n;

/** v4 escrow ABI: Job tuple drops `evaluator`, adds committeeSize+quorum; plus committee/vote reads + events. */
export const escrowAbiV4 = [
  {
    type: "function", name: "getJob", stateMutability: "view",
    inputs: [{ name: "jobId", type: "uint256" }],
    outputs: [{
      name: "", type: "tuple",
      components: [
        { name: "client", type: "address" },
        { name: "provider", type: "address" },
        { name: "amount", type: "uint256" },
        { name: "specHash", type: "bytes32" },
        { name: "deliverableHash", type: "bytes32" },
        { name: "irysId", type: "string" },
        { name: "deadline", type: "uint64" },
        { name: "status", type: "uint8" },
        { name: "committeeSize", type: "uint8" },
        { name: "quorum", type: "uint8" },
      ],
    }],
  },
  { type: "function", name: "nextJobId", stateMutability: "view", inputs: [], outputs: [{ type: "uint256" }] },
  { type: "function", name: "getCommittee", stateMutability: "view", inputs: [{ name: "jobId", type: "uint256" }], outputs: [{ type: "address[]" }] },
  {
    type: "function", name: "getVote", stateMutability: "view", inputs: [{ name: "jobId", type: "uint256" }],
    outputs: [
      { name: "approveCount", type: "uint8" }, { name: "rejectCount", type: "uint8" },
      { name: "votingDeadlineBlock", type: "uint64" }, { name: "tentativePayout", type: "bool" },
      { name: "resolvedBlock", type: "uint64" },
    ],
  },
  { type: "event", name: "JobCompleted", inputs: [{ name: "jobId", type: "uint256", indexed: true }, { name: "provider", type: "address", indexed: true }, { name: "amount", type: "uint256", indexed: false }] },
  { type: "event", name: "JobRejected", inputs: [{ name: "jobId", type: "uint256", indexed: true }, { name: "client", type: "address", indexed: true }, { name: "amount", type: "uint256", indexed: false }] },
  { type: "event", name: "RefundClaimed", inputs: [{ name: "jobId", type: "uint256", indexed: true }, { name: "client", type: "address", indexed: true }, { name: "amount", type: "uint256", indexed: false }] },
  { type: "event", name: "VoteCast", inputs: [{ name: "jobId", type: "uint256", indexed: true }, { name: "member", type: "address", indexed: true }, { name: "approve", type: "bool", indexed: false }, { name: "newCount", type: "uint8", indexed: false }] },
  { type: "event", name: "JobResolved", inputs: [{ name: "jobId", type: "uint256", indexed: true }, { name: "tentativePayout", type: "bool", indexed: false }, { name: "approveCount", type: "uint8", indexed: false }, { name: "rejectCount", type: "uint8", indexed: false }] },
  { type: "event", name: "JobDisputed", inputs: [{ name: "jobId", type: "uint256", indexed: true }, { name: "disputer", type: "address", indexed: true }, { name: "committeePayout", type: "bool", indexed: false }] },
  { type: "event", name: "DisputeResolved", inputs: [{ name: "jobId", type: "uint256", indexed: true }, { name: "payProvider", type: "bool", indexed: false }] },
] as const;
