# AgentWorks ACP evaluator agent (Model A)

Makes the **AgentWorks M-of-N committee** the evaluator behind a single ACP `evaluatorAddress`. On each
`job.submitted`, this Node agent fetches the job's spec + deliverable, asks the AgentWorks committee for a
verdict (the Python `POST /acp/verdict` service), and settles the ACP job — `session.complete` (pay provider)
or `session.reject` (refund client). ACP's own escrow settles; AgentWorks contracts are not in the money path.

Full design: `docs/ACP_ADAPTER.md`. This folder mirrors `agents/irys/` — a standalone Node package inside
`agents/`, **outside the pnpm workspace** (only `web` is a workspace member).

## Prerequisites
1. **Run the verdict service** — the AgentWorks Python API with the committee configured:
   ```
   cd agents && ./.venv/Scripts/python.exe server.py     # exposes POST /acp/verdict
   ```
   (Needs `CAW_EVALUATOR_*` env for the committee; falls back to a single judge if none. Set `ACP_VERDICT_TOKEN`
   to gate it, and mirror that value in this agent's `.env`.)
2. **Register the evaluator on Virtuals** — https://app.virtuals.io/acp/new: set up the agent profile, then the
   **Signers** tab → **+ Add Signer** → **Copy Key**. You get a `walletAddress`, a `walletId`, and a signer
   private key. Fund that wallet with gas on the sandbox chain (Base Sepolia ETH).

## Setup
```
cd agents/acp-node
npm install
cp .env.example .env      # fill WALLET_ADDRESS, WALLET_ID, SIGNER_PRIVATE_KEY, AGENT_API
npm start                 # node evaluator.mjs
```

## SDK API — confirmed against `@virtuals-protocol/acp-node-v2@0.1.9`
`evaluator.mjs` is written against the SDK's actual dist types (verified, not guessed):
- `AcpAgent.create({ evmProvider })` · `PrivyAlchemyEvmProviderAdapter.create({ walletAddress, walletId, signerPrivateKey, chains: [baseSepolia] })` · `agent.on("entry", (session, entry) => …)` · `agent.start(onConnected)`.
- Detect submissions: `entry.kind === "system" && entry.event.type === "job.submitted"` → the deliverable is `entry.event.deliverable`.
- The **spec** is the room message with `contentType === "requirement"` in `session.entries`; role gate via `session.roles.includes("evaluator")`; settle with `session.complete(reason)` / `session.reject(reason)`.
- `baseSepolia` (chain 84532) from `viem/chains`.

The **only** thing left to validate is live runtime behavior in the ACP Sandbox (below) — that needs the Virtuals
registration + a real submitted job.

## Prove it (ACP Sandbox)
Agents start in the ACP **Sandbox**. Drive two sandbox jobs that name this evaluator:
- one whose deliverable satisfies the spec → committee **approves** → `complete` → provider paid;
- one that doesn't → committee **rejects** → `reject` → client refunded.
Capture the ACP job ids; that's the Week-3 proof (record in `docs/ACP_ADAPTER.md`).

## Deferred
A CAW-signer adapter (MPC can't hand over a raw key, so this uses the Virtuals/Privy signer) and the
staked-dispute/UMA "appeals court" (that stays on AgentWorks' native rails — Model B). See `docs/ACP_ADAPTER.md`.
