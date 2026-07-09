# Consensus evaluation + staked disputes (escrow v4)

AgentWorks v1–v3 settled each job through a **single evaluator** address named at `createJob`: it alone
gated `complete`/`reject`. That is a single point of failure — a hallucinating, offline, or compromised
evaluator could drain escrow to a bad provider or unfairly refund a client. **Escrow v4**
(`contracts/src/AgentWorksEscrowV4.sol`) removes it two ways, while keeping the v3 sealed commit-reveal
accept verbatim.

## 1. M-of-N committee consensus

`createJob(address[] evaluators, uint8 quorum, …)` names an **odd committee** (N ≤ 7) with a
**strict-majority quorum** (default 3 evaluators, quorum 2). After the provider `submitWork`s:

- each committee member independently pulls the deliverable from Irys, judges it with its **own LLM
  reasoning** (distinct personas — correctness / completeness / usefulness — so votes are genuinely
  arrived at, not echoed), and calls `castVote(jobId, approve)`;
- reaching the quorum on either side moves the job to **`Resolved`** with a **tentative** outcome — and
  **no funds move yet**;
- if neither side reaches quorum by the voting deadline, anyone calls `forceResolve` → tentative
  **refund** (the provider only earns by convincing a majority; the conservative default returns
  principal to the funder, and the provider may still escalate via dispute).

A committee member can never move USDC — the `evaluator_pact` allowlists only `castVote` on the escrow,
USDC excluded (`docs/pacts/evaluator_pact_v4.json`). Settlement is the contract's, after quorum + the
dispute window — never an evaluator's.

## 2. Staked dispute → decentralized arbiter (no operator key)

A tentative `Resolved` outcome opens a **dispute window** (block-number based, immutable ctor param):

- **No dispute** → anyone calls `finalize(jobId)` after the window → the tentative outcome executes
  (payout or refund), CEI-safe.
- **Dispute** → the **losing side** stakes a **bond** and calls `dispute(jobId)`, which hands off to the
  decoupled arbiter. Status → `Disputed`. The escrow holds no bond; the stake lives at the arbiter
  (the UMA assertion bond), which is the correct place for it.
- **Ruling** → only the `arbiter` address can call `resolveDispute(jobId, payProvider)` — executes the
  final payout/refund. **There is no operator-EOA path**: a single admin key deciding disputes would
  defeat the trustless premise, so the escrow structurally forbids it.
- **Arbiter downtime** → `resolveTimeout(jobId)` (permissionless, after a resolve deadline) executes the
  committee's *original* tentative outcome and frees the job. This only ever enacts what the committee
  already decided — it is an anti-freeze backstop, never an arbitrary ruling — so it is not a
  centralization vector.

### The decoupling seam (`IArbiter`)

The escrow knows only an immutable `arbiter` *address* implementing `IArbiter.openDispute(...)`, and only
accepts `resolveDispute` back from it. The arbiter is therefore **pluggable** with **zero escrow
changes** — swap one adapter contract for another at deploy time.

### The live adapter: UMA Optimistic Oracle V3 (`AgentWorksUmaArbiter.sol`)

The deployed arbiter is a **real decentralized-oracle adapter** integrating **UMA's Optimistic Oracle V3**,
live on Ethereum Sepolia. The ruling authority is UMA's economic oracle, not any key:

1. `dispute()` → the adapter pulls the disputer's bond (a UMA-whitelisted currency) and calls
   `OOv3.assertTruth("AgentWorks job #N: pay the provider = <disputer's claim>")` with a configurable
   liveness.
2. If the assertion is **not** counter-disputed within liveness, anyone calls `settle` → UMA invokes
   `assertionResolvedCallback(assertionId, true)` → the adapter calls `escrow.resolveDispute(...)` in the
   disputer's favour and returns the bond.
3. If it **is** counter-disputed, UMA's DVM resolves it (on mainnet) and the same callback fires.

**Live deployment (Sepolia, verified):**
- `AgentWorksEscrowV4` (live, hardened): `0x17f58B3DcCad608867F19A88499f0F11C5F9b5bA` (deploy block 11199179;
  votingWindow 600, disputeWindow 50, resolveWindow 50 blocks; resolveWindow coupled on-chain to the arbiter
  liveness so `resolveTimeout` can never preempt an honest UMA ruling)
- `AgentWorksUmaArbiter` (live): `0x850121Aa89C1C6d759F2751E01e8888e412a7a42` (its `arbiter`)
- Previous v4 (committee→payout proofs): escrow `0x8F60e34e43Dd53Bd170633fB5b1d8c43e21C264C` · arbiter
  `0x8BDB79EB6cDC3E54E373C0E5096CffD737a5DE4B`
- UMA OOv3: `0xFd9e2642a170aDD10F53Ee14a93FcF2F31924944` · bond currency `6TEST`
  `0x3870419Ba2BBf0127060bCB37f69A1b1C090992B` (UMA-whitelisted, 6-dp) · bond + liveness are ctor params.
- Previous v4 (identical bytecode, superseded only to widen the voting window 50→600 blocks; retained
  because the live dispute below settled on it): escrow `0x86B422CC8F75B7c5521a2552F2C34da8cb342C86`,
  arbiter `0xd933a3816E6b0818e0EEEb4f4776dA9157172755`.

### Demonstrated live on Sepolia

**Committee → finalize (live hardened escrow `0x17f5…b5bA`, job #1):** a 3-evaluator committee on **3 independent
CAW wallets** (quorum 2) judged the deliverable and `castVote`d on-chain 2-0 — `A 0x53c5b0ef…`, `C
0x80080d01…` → tentative `Resolved` (no funds move) → after the dispute window anyone `finalize`d
(`0xcae19436…`) → `Completed`, 5 USDC to the provider. (The original flagship — same flow — settled on the
previous v4 `0x8F60…264C`: votes `0xadf6546d…`/`0x8d1e8e07…`, finalize `0xcb57533b…`.)

**Committee → staked dispute → UMA (previous v4 `0x86B4…2C86`, job #2):** a full staked dispute ran
end-to-end against the real UMA OOv3, **no operator key**: committee resolved tentative **payout** → the
losing side (client) staked the bond and `dispute`d → the adapter posted a real UMA assertion
(`assertionId 0x26d55b3f…`) → after liveness anyone `settle`d → UMA's `assertionResolvedCallback` →
`escrow.resolveDispute` → the outcome was **overturned to refund** (`Rejected`). Txs: dispute
`0x143a0531…`, settle (UMA → resolveDispute) `0x8e40fdc9…`. (Because the escrow bytecode is identical, this
proof carries to the live escrow unchanged.)

### Network note (a UMA property, not an AgentWorks gap)

UMA's Sepolia OOv3 has **no DVM**, so the **optimistic** path (assert + liveness, undisputed) settles live —
which is exactly what we demonstrate. A *counter-disputed* assertion is escalated to UMA's DVM, which exists
**only on mainnet**. Production is therefore a **one-env-var change** — point `UMA_OOV3_ADDRESS` at UMA's
mainnet OOv3 and `UMA_BOND_CURRENCY` at real USDC (with a meaningful `UMA_BOND`, e.g. 400 USDC, and
hour-scale liveness) — with **zero escrow/adapter code change**. `assertionDisputedCallback` already handles
the DVM-escalated branch. The bond/liveness/windows are all immutable ctor params, set per deploy.

### Alternate adapter: Kleros (ERC-792)

`IArbiter` can equally wrap a **Kleros** arbitrable (the ERC-792 `IArbitrator`/`IArbitrable` standard) —
a decentralized juror court. It isn't confirmed deployed on Sepolia, so UMA is the live adapter; Kleros
is the documented drop-in alternative the same seam accepts.

## 3. Honest risk assessment

- **Committee collusion** — a corrupt quorum can force either outcome; this is the irreducible trust of
  any M-of-N vote. Mitigations: odd N + strict majority raises the threshold, and the staked dispute
  gives the harmed party recourse to the (decentralized) arbiter even against a colluding committee. v4
  does not slash evaluators; collusion deterrence comes from the escalation path. Production would draw
  the committee from a reputation/stake-weighted pool (out of scope; the seam supports it).
- **Arbiter trust** — the arbiter is the UMA oracle (economic security), *not* an admin key.
  `resolveTimeout` bounds its power: it cannot freeze funds by going silent.
- **Bond griefing** — a frivolous dispute costs the loser the bond (forfeited at the oracle layer);
  the only residual is the bounded resolve-window delay, ended by `resolveTimeout`.
- **Fund stranding** — every state has a timed exit (`forceResolve`, `finalize`, `resolveDispute`,
  `resolveTimeout`, `claimRefund`); no state traps principal. Foundry asserts the escrow balance reaches
  zero after settlement in every branch.
- **Gas** — `MAX_COMMITTEE = 7` bounds the `createJob` loop; `castVote` is O(1). No unbounded iteration.

## 4. Evidence

- **Foundry:** `contracts/test/AgentWorksEscrowV4.t.sol` (63 tests) + `AgentWorksUmaArbiter.t.sol`
  (10 tests, against `MockOptimisticOracleV3`). Suite-wide **186 tests** pass.
- **Live (Sepolia):** v4 + the UMA adapter deployed + verified; wiring confirmed (`escrow.arbiter()` ==
  adapter, `adapter.oo()` == UMA OOv3). **Both settlement paths demonstrated live** — committee→finalize
  payout (job #1 `Completed`) and committee→**staked dispute→UMA→refund** (job #2 `Rejected`, UMA assertion
  `0x26d55b3f…`). The committee itself was then run **through CAW** (job #4: `castVote`s signed by the
  evaluator wallet under `evaluator_pact`, USDC denied — see §5). Tx hashes in [ARCHITECTURE.md § Verified on-chain](ARCHITECTURE.md#verified-on-chain-full-proof-set).

## 5. The committee through CAW (demonstrated live)

The committee's votes are CAW `contract_call`s from a dedicated **Evaluator CAW wallet** (one TSS-paired,
agent-owned wallet hosting the committee addresses `CAW_EVALUATOR_ADDRESS_1..N`), each bound by the
`evaluator_pact` (castVote-only, **USDC excluded**). This ran **end-to-end, fully hands-off** on Sepolia
(previous v4 `0x86B4…2C86`, identical bytecode to the live escrow): the client funded **job #4** naming the 3 evaluator addresses, the provider
delivered, and **each committee member `castVote`d through CAW** — Evaluator A
([`0x959be72a…`](https://sepolia.etherscan.io/tx/0x959be72af5407771c11dce123fcf45e45e75769fe0365a957d00851e9a6ef6db)),
Evaluator B
([`0xc807f98d…`](https://sepolia.etherscan.io/tx/0xc807f98dab59b9f1d0a8cbbff7bc4d5c73fe9b8d162862db708795b078923d94))
→ quorum 2-of-3 → tentative `Resolved` (no funds moved) →
[`finalize 0xd6b8e9fc…`](https://sepolia.etherscan.io/tx/0xd6b8e9fc3624bf558a3042b33758fd9671cd24ed9f4a52916cdb71442b5d8b24)
→ **Completed** (payout). The authority boundary holds: the same evaluator wallet attempting to move USDC
under its Pact is **denied by CAW** (`CONTRACT_NOT_WHITELISTED`, 403) — a committee member can vote but can
**never** touch escrow. (Committee casts are serialized so the quorum-reaching vote, which triggers
`_resolve`, is gas-estimated against current chain state.)

On the **live hardened escrow** (`0x17f5…b5bA`, job #1) the committee ran across **three independent CAW wallets**
(one per evaluator, not one shared wallet) — `A 0x53c5b0ef…`, `C 0x80080d01…` reached quorum 2-of-3 →
`finalize 0xcae19436…` → payout — the strongest form of the same trustless boundary.

The three demo addresses share one Evaluator wallet / Pact / TSS node — a genuine 3-member committee without
three daemons, mirroring the provider race. In **production** each committee seat is an **independent
external evaluator operator** running its own CAW wallet via the MCP server (`MCP_ROLE=evaluator`) — the same
trustless self-onboarding proven for client/provider — so no single operator runs the whole committee (that
would be collusion). The seam is identical; only the number of wallets differs.
