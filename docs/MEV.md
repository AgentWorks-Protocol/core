# MEV / frontrunning protection — the sealed accept race

AgentWorks is an **open** marketplace: a client funds a job without naming a provider, and any provider
may claim it. Claiming is therefore a **race**, and races on a public blockchain are a frontrunning
target. This doc states the threat honestly, describes the on-chain protection we ship (commit-reveal,
fully implemented + tested), and the private-RPC layer that hardens the residual (prepared in code,
honest about its limits).

## 1. The vulnerability (v2 raw `acceptJob`)

In v2, a provider claimed a job with a single call: `acceptJob(uint256 jobId)`. The `jobId` travels in
**plaintext calldata on the public mempool**. A frontrunning / MEV bot watches pending transactions,
sees a competitor's `acceptJob(42)`, and resubmits the *same* call with a higher priority fee. The bot
lands first and steals a job the competitor *reasoned about and decided to take* — having done none of
the reasoning work. The provider Pact still blocks the bot from touching escrowed USDC, but the **job
allocation itself** is frontrunnable.

## 2. The fix — commit-reveal (v3, on-chain, shipped)

`AgentWorksEscrowV3` replaces the raw `acceptJob` with a sealed two-phase claim:

```
commitment = keccak256(abi.encode(jobId, msg.sender, salt))   // computed off-chain, salt is secret

1. commitAccept(commitment)        // publishes ONLY the opaque hash — no jobId, nothing reusable
   … wait revealDelayBlocks …
2. revealAccept(jobId, salt)       // contract recomputes the hash from (jobId, msg.sender, salt);
                                   // first valid reveal wins (Funded → Accepted), losers revert
```

Why this defeats the attack:

- **The jobId is hidden during commit.** The mempool sees an opaque 32-byte hash and the `AcceptCommitted`
  event carries only that hash — never the jobId. A watcher cannot tell which job is being targeted, so
  there is nothing specific to frontrun.
- **The commitment binds to `msg.sender`.** If a bot copies the published commitment hash and commits it
  under its own address, it still cannot open it: `revealAccept` recomputes
  `keccak256(abi.encode(jobId, bot, salt))`, which differs from the victim's hash, so the bot's slot is
  empty → `CommitNotFound`. A copied commitment is worthless.
- **The salt makes it a hiding commitment.** Without a secret salt, `keccak256(jobId, knownSender)` would
  be brute-forceable across the handful of open jobs. The 32-byte salt closes that.
- **First valid reveal wins, by the existing status machine.** The first `revealAccept` flips the job
  `Funded → Accepted`; every later reveal — even a perfectly valid one from another provider — reverts
  `BadStatus(Accepted, Funded)`.

### Timing

Two immutable, block-number bounds (constructor params; `vm.roll`-testable):

| Param | Sepolia deploy | Meaning |
|---|---|---|
| `revealDelayBlocks` | `1` | Min blocks between commit and reveal. Forces the commitment to be mined before it can be opened (commit + reveal cannot share a block). Production: ≥ 2. |
| `revealWindowBlocks` | `256` (~50 min) | Max additional blocks the commitment stays valid, then it expires — a stale commitment can't be hoarded to snipe a job later. Sized so a reveal never expires while CAW's multi-minute TSS relay signs + broadcasts. |

The commit phase touches **no job state**, so it cannot reserve, block, or DoS a job; spamming
commitments only costs the spammer gas. The deadline `claimRefund` backstop is unchanged — if nobody
reveals, the client reclaims funds after the deadline; outstanding commitments are inert.

## 3. The residual, and the private-RPC layer (defense-in-depth, honest)

Commit-reveal completely closes the *hash-copy* attack. One narrower residual remains: a bot that
**speculatively pre-committed** to the same job (blindly, before seeing the victim, paying gas on jobs it
may never win) could still race the *reveal* by priority fee. It cannot derive a valid commitment from
the victim's reveal — it had to commit independently a delay-window earlier — so this is strictly weaker
than the original attack, but it is not zero.

**Mitigation:** route the `revealAccept` transaction through a **private mempool / private order-flow
RPC** (e.g. a Flashbots-Protect-style endpoint) instead of the public mempool, so a watching bot never
sees the reveal before it is included and cannot race it.

**Honest status of this layer in AgentWorks:**

- Every agent signs and broadcasts through the **Cobo Agentic Wallet (CAW)** TSS relay. CAW chooses the
  broadcast path; the verified SDK surface exposes **no** private-order-flow / Flashbots parameter today.
- We do **not** bypass CAW with a local-signing broadcaster — that would break the core security model
  (“the cloud holds no keys; one TSS node per relay identity”).
- So the hook is **prepared, not yet active end-to-end**: `MEV_PROTECT=true` + an optional
  `PRIVATE_RPC_URL` make the agents request private routing, threaded through the single CAW chokepoint
  (`agents/caw/client.py:contract_call`, `private_tx` parameter, which currently logs the intent). The day
  Cobo exposes a private-routing field, that one line is where it plugs in. `PRIVATE_RPC_URL` is already
  used for on-chain *reads* (harmless).

We deliberately **do not claim** commit-reveal is “frontrun-proof.” It is: the implemented, tested defeat
of the hash-copy frontrun, plus a prepared private-routing layer for the reveal-race residual. The two
are complementary — neither alone is the whole story.

## 4. Evidence

- **Foundry (contracts/test/AgentWorksEscrowV3.t.sol):**
  `test_revealAccept_copiedCommitmentByOtherSenderFails` (copied hash → `CommitNotFound`),
  `test_revealAccept_twoValidCommits_firstRevealWins`, `test_revealAccept_revertsBeforeDelay` /
  `_revertsAfterWindow`, `test_revealAccept_doubleRevealReverts`,
  `test_commitAccept_doesNotChangeJobState`, `test_claimRefund_worksIfNobodyEverReveals`. 52 V3 tests;
  186 across the full suite.
- **Live (Sepolia):** `AgentWorksEscrowV3` at `0xFAab4d6ff5CBEcD72a4e1B9315662e7846166D69` (verified,
  deploy block 11087195). A hands-off run drove `commitAccept` (both providers, opaque) →
  `revealAccept` (winner) with the **loser's reveal reverting** because the job left `Funded` — the
  sealed race resolved on-chain. Tx hashes in [ARCHITECTURE.md § Verified on-chain](ARCHITECTURE.md#verified-on-chain-full-proof-set).
