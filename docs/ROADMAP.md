# Roadmap — Base mainnet + Virtuals/ACP

AgentWorks is live on Ethereum Sepolia today. The next milestone is **Base mainnet with real USDC**, positioned
as the trustless settlement rail for **Virtuals Protocol's Agent Commerce Protocol (ACP)**. This is the concrete
launch plan; the contracts are chain-agnostic EVM and port untouched — the work is configuration, one contract
constant, and a real-money safety pass, not a rewrite.

## Why Base

AgentWorks leans on two external services + real money + cheap gas, so a launch chain must clear four gates:

1. **Cobo CAW support** — the authority layer; non-negotiable. CAW mainnets: **Ethereum, Base, Arbitrum,
   Optimism, Polygon, BNB, Avalanche C-Chain, HyperEVM, Solana** (testnets Sepolia, Base Sepolia, Solana Devnet).
2. **UMA Optimistic Oracle V3** — the dispute arbiter behind the `IArbiter` seam. Live on Ethereum + Base + the
   major L2s.
3. **Canonical (native) USDC + liquidity + real users.**
4. **Low fees / fast finality** — a job is ~7 txs (createJob, fund, commit, reveal, submit, N votes, finalize),
   so L1 Ethereum is out; we need an L2.

**Base clears all four, and it's where the agent demand is** (the Virtuals ecosystem). It's the launch chain.

## Launch plan — Base mainnet

Everything hardened on Sepolia (claimRefund fix, resolve-window↔liveness coupling, sealed commit-reveal, M-of-N
committee, staked UMA disputes) ports as-is. What changes is config + one contract constant:

| Piece | Sepolia (now) | Base mainnet |
|---|---|---|
| CAW chain id (`CAW_CHAIN_ID`) | `SETH` | `BASE_ETH` (dry-run on `TBASE_SETH` first) |
| USDC token id / address | MockUSDC `0x4C4D…D910` | **native `BASE_USDC`** (Circle) |
| Escrow v4 + arbiter | `0x17f5…b5bA` / `0x8501…7a42` | redeploy via `DeployV4.s.sol` |
| `IArbiter` arbiter | UMA OOv3 (Sepolia) | **UMA OOv3 on Base** (unchanged seam) |
| UMA bond currency | `6TEST` `0x3870…` | a UMA-whitelisted currency on Base |
| Dashboard config | `web/lib/config.ts` defaults | new addrs + `NEXT_PUBLIC_*` on Vercel |
| Agent service env | droplet `agent.env` | flip chain/token/addr envs |

### Phases

**Phase 0 — Contract constant fix (gating; must land before any Base deploy).**
`AgentWorksEscrowV4` hardcodes `SECONDS_PER_BLOCK = 12` (Ethereum-L1 slot time) in the invariant
`resolveWindowBlocks × SECONDS_PER_BLOCK ≥ arbiter.liveness()`. **Base blocks are ~2s**, so the same block count
spans far less real time than the guard assumes — on Base the invariant *under-protects*. Fix before deploy:
either lower the constant to Base's block time or express the resolve window in seconds. Cheap, but a hard
prerequisite — a new `contracts/test/` case should assert the window covers liveness in real seconds at Base's
block time. **This is the one code change the launch needs.**

**Phase 1 — Base Sepolia dry-run (`TBASE_SETH`).** CAW supports it. Deploy v4 + arbiter, run one committee →
finalize payout and one committee → staked dispute → UMA on a Base-family chain to confirm the whole flow
end-to-end before touching mainnet. Confirm Base's exact OOv3 address + a whitelisted bond currency from the
[UMA network addresses](https://docs.uma.xyz/resources/network-addresses) (do not hardcode from memory).

**Phase 2 — Base mainnet deploy.** `DeployV4.s.sol` with the Base RPC + Base UMA OOv3 + **native Circle USDC** as
the escrow token; pick voting/dispute/resolve windows against the corrected block-time constant; re-verify on
Basescan.

**Phase 3 — Rewire.** `config.py`, `.env`, `web/lib/{config,abi}.ts`, agent `agent.env`, and the docs — the same
pattern as the Sepolia hardened redeploy (chain the old deployment as `escrowV4Prev` so historical jobs still
resolve).

**Phase 4 — Real-money safety pass.** This is mainnet with real USDC. Re-confirm the Pact spend caps, the
`AGENT_TRIGGER_TOKEN` gate (must be set), and the marketplace write-endpoint hardening are all on. Start with
conservative per-job caps and a small committee bond; widen only after live jobs settle cleanly.

## The Virtuals / ACP play

Virtuals is an agent-tokenization ecosystem on Base; its **Agent Commerce Protocol (ACP)** is the framework for
agents to transact with each other. It's adjacent to us, which is exactly why it fits:

- **They have supply** — many tokenized agents that need to pay and get paid.
- **We have the safe money rail** they don't natively provide: escrow + sealed accept race + M-of-N committee
  settlement + staked UMA disputes + **CAW Pact-bounded spend authority**.

The play is to **be the trustless settlement/escrow-with-guardrails layer for ACP agents**, not a competitor. An
ACP job escrows through AgentWorks; our committee + dispute flow settles it; the CAW Pact guarantees no agent —
compromised, hallucinating, or adversarial — exceeds its spend bounds. The pitch is the piece ACP lacks: *"agents
transact — but who stops a compromised agent from draining funds, and who adjudicates a disputed deliverable?"*

**To validate before committing integration engineering:** does ACP already ship its own escrow/settlement? If
so we're an alternative or an integration — and we win on the **safety story** (spend-bounding + M-of-N +
decentralized dispute). Confirm what ACP settlement looks like today, and whether there's a partnership path.

## Beyond launch

- **Reputation / stake-weighted committee selection** from a larger evaluator pool (the `IArbiter` + committee
  seams already support it).
- **DVM-escalated disputes.** On Base mainnet UMA's full dispute court (DVM) settles contested assertions;
  Sepolia is optimistic-only, so the contested branch is a mainnet property we inherit for free, not new work.
- **A fully public marketplace** — rate limits + a registration approval queue on top of the existing
  bearer-token gates (the external-agent endpoints + volume-backed persistence are already in place).
- **Multi-chain.** The modular architecture (one CAW integration file, the `IArbiter` seam) makes adding a chain
  config + redeploy, not a rewrite. Candidate #2 chains are gated on CAW support + a UMA (or alternate-arbiter)
  deployment.

### BOT Chain (botchain.ai) — a conditional later expansion, not the launch

BOT Chain is an EVM-compatible agent-economy L1 (~0.75s blocks, near-zero fees, native agent identity `AIDID`).
Attractive on paper — cheapest fees, purest agent-chain narrative — but it **fails our two load-bearing gates
today**: no Cobo CAW (deploying there means shipping the escrow without Pact-bounded authority — our
differentiator), and no UMA OOv3 (would need an alternate arbiter, e.g. a Kleros ERC-792 adapter, behind the
`IArbiter` seam). USDC there is bridged, not canonical, and independent liquidity/activity data is thin. **Revisit
only if** (1) CAW adds it, (2) we've built + tested an alt-arbiter adapter, and (3) it shows real USDC +
liquidity + users.

## Open diligence items

1. **Cobo:** is CAW Base fully GA for agentic wallets? Any chain-roadmap notes relevant to us.
2. **UMA on Base:** exact OOv3 address + a whitelisted, liquid bond currency (real USDC?).
3. **Virtuals/ACP:** does ACP already have escrow/settlement? Replace, wrap, or complement — and is there a
   partnership/integration path?
4. **Base block time vs the `SECONDS_PER_BLOCK` invariant** — Phase 0, above.

## Sources
- CAW supported chains — Cobo CAW `chains-and-tokens` reference.
- UMA OOv3 — https://docs.uma.xyz/developers/optimistic-oracle-v3 · addresses
  https://docs.uma.xyz/resources/network-addresses
- Virtuals / ACP — verify current ACP settlement design before executing.
