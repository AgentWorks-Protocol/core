# contracts - AgentWorks escrow (Foundry)

The neutral settlement layer for the autonomous open marketplace. Funds are held by neither agent; only the
contract moves them, and only along the lifecycle (with a deadline refund as the backstop). The live escrow is
**v4**: a sealed commit-reveal accept (anti-frontrunning) + **M-of-N committee consensus** evaluation + a
**staked dispute** window escalating to a decoupled, decentralized arbiter (UMA OOv3 — no operator key):
`createJob(committee) → fund → commitAccept → revealAccept → submitWork → castVote ×N → Resolved → finalize | dispute → resolveDispute | resolveTimeout`.

- [`src/AgentWorksEscrowV4.sol`](src/AgentWorksEscrowV4.sol) - **the live escrow (committee consensus + staked
  disputes).** Drops v3's single `evaluator` for an odd `address[] evaluators` committee + strict-majority
  `quorum`; `castVote` tallies → tentative `Resolved` (no funds move) → `finalize` (no dispute) or `dispute`
  (staked) → `resolveDispute` (arbiter-only) / `resolveTimeout` (anti-freeze). The `arbiter` is an immutable
  `IArbiter` address — never an operator EOA. Keeps the v3 sealed commit-reveal accept verbatim. See
  [../docs/ARBITRATION.md](../docs/ARBITRATION.md).
- [`src/AgentWorksUmaArbiter.sol`](src/AgentWorksUmaArbiter.sol) - the real **UMA Optimistic Oracle V3** adapter
  that IS the escrow's `arbiter`; disputes are ruled by UMA's economic oracle. Swappable for a Kleros ERC-792
  adapter with zero escrow changes.
- [`src/AgentWorksEscrowV3.sol`](src/AgentWorksEscrowV3.sol) - prior v3: sealed commit-reveal accept (the
  jobId stays off the public mempool; a copied commitment is useless), single evaluator. Kept for history; its
  accept mechanism is carried into v4. See [../docs/MEV.md](../docs/MEV.md).
- [`src/MockUSDC.sol`](src/MockUSDC.sol) - 6-decimal mintable ERC-20, the deterministic settlement token.
- [`src/AgentWorksEscrowV2.sol`](src/AgentWorksEscrowV2.sol) - the prior v2 (raw `acceptJob` race), kept for history.
- [`src/AgentWorksEscrow.sol`](src/AgentWorksEscrow.sol) - the prior v1 (closed 1:1) escrow, kept for history.
- [`test/AgentWorksEscrowV4.t.sol`](test/AgentWorksEscrowV4.t.sol) (63) + [`test/AgentWorksUmaArbiter.t.sol`](test/AgentWorksUmaArbiter.t.sol)
  (10, against `MockOptimisticOracleV3`) + V3/V2/V1 files for history — **186 tests total**: committee
  validation/voting/quorum, tentative resolve, finalize, staked dispute, arbiter ruling (all overturn/uphold
  combos), resolve-timeout anti-freeze, the sealed commit-reveal race, expiry refund, access/status guards, CEI.
- [`script/DeployV4.s.sol`](script/DeployV4.s.sol) - deploys the UMA arbiter adapter (wired to live OOv3) then
  escrow v4 pointed at it. DeployV3/DeployV2/DeployMockUSDC remain.

## Live addresses (Ethereum Sepolia, chainId 11155111, all verified on Etherscan)
- Escrow v4 (LIVE, committee + disputes; hardened): `0x17f58B3DcCad608867F19A88499f0F11C5F9b5bA` (deploy block 11199179; votingWindow 600 / disputeWindow 50 / resolveWindow 50 blocks; claimRefund excludes Submitted + resolveWindow coupled on-chain to the arbiter liveness)
- UMA arbiter adapter (the escrow's `arbiter`): `0x850121Aa89C1C6d759F2751E01e8888e412a7a42`
- Previous v4 (superseded by the hardened redeploy; committee→payout proofs settled here): escrow `0x8F60e34e43Dd53Bd170633fB5b1d8c43e21C264C` · arbiter `0x8BDB79EB6cDC3E54E373C0E5096CffD737a5DE4B`
- Previous v4 #2 (identical bytecode; live dispute→UMA proof settled here): escrow `0x86B422CC8F75B7c5521a2552F2C34da8cb342C86` · arbiter `0xd933a3816E6b0818e0EEEb4f4776dA9157172755`
- Escrow v3 (legacy, commit-reveal): `0xFAab4d6ff5CBEcD72a4e1B9315662e7846166D69`
- Escrow v2 (legacy, raw acceptJob): `0xD6cB413c0E4a5839Fd4B02aFFeBF65e6868726b9`
- MockUSDC: `0x4C4D1223BcC47E380CF4C37652EaDFe10A9Fd910` · UMA OOv3: `0xFd9e2642a170aDD10F53Ee14a93FcF2F31924944`

## Usage

Foundry lives at `~/.foundry/bin` (not on PATH on this machine). Env vars come from the repo-root `.env`.

```bash
forge build
forge test -vv                       # 186 tests
forge test --gas-report

# Deploy escrow v4 + UMA arbiter to Ethereum Sepolia (DEPLOYER_PRIVATE_KEY funded with Sepolia ETH):
set -a; . ../.env; set +a
forge script script/DeployV4.s.sol \
  --rpc-url "$RPC_URL" --broadcast \
  --verify --verifier etherscan --etherscan-api-key "$EXPLORER_API_KEY" --chain 11155111
```

> `lib/` (forge-std) is gitignored; run `git clone --depth 1 https://github.com/foundry-rs/forge-std lib/forge-std`
> on a fresh checkout.
