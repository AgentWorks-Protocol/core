# Org restructure — status

Turning this repo into the **[AgentWorks-Protocol](https://github.com/AgentWorks-Protocol)** GitHub org, with the
Virtuals adapter as its own repo.

Target shape:
```
AgentWorks-Protocol/
├── core              ← this repo (contracts + agents + web + docs)          [transfer = YOUR step]
├── virtuals-adapter  ← the extracted Virtuals/ACP evaluator adapter          ✅ created
└── .github           ← org profile (renders on the org page)                 ✅ created
```

## ✅ Done (driven automatically)
- **`virtuals-adapter`** created + pushed → https://github.com/AgentWorks-Protocol/virtuals-adapter (private).
  Contains the extracted adapter (`evaluator.mjs`, `twoagent.mjs`, `package.json`, `README`, `.env.example`) +
  `ACP_ADAPTER.md` + the private Virtuals partnership/launch notes. It talks to the core only over HTTP
  (`POST {AGENT_API}/committee/verdict`). No secrets were copied (`.env` / `node_modules` excluded).
- **`.github`** org profile created + pushed → https://github.com/AgentWorks-Protocol/.github (public);
  renders on https://github.com/AgentWorks-Protocol.
- **`core`** de-Virtualsed + repositioned + the adapter removed (`git rm agents/acp-node`), committed + pushed to
  the current origin. README/INTEGRATIONS links now point at the org repos.

## ⏳ Left for you
1. **Transfer `core` into the org** (moves your personal repo; keeps history + sets up GitHub redirects so old
   links keep working):
   ```bash
   gh api -X POST repos/Manuel-dev01/AgentWorks/transfer -f new_owner=AgentWorks-Protocol
   gh repo rename core -R AgentWorks-Protocol/AgentWorks       # → AgentWorks-Protocol/core
   git remote set-url origin https://github.com/AgentWorks-Protocol/core.git
   ```
2. **Re-point Vercel** to `AgentWorks-Protocol/core` (Project → Settings → Git) after the transfer. The landing
   copy already changed, so the next deploy ships the repositioned page. Until you transfer, the live footer's
   GitHub links point at `Manuel-dev01/AgentWorks` (they keep working, and auto-redirect after transfer).
3. **(Optional) flip `virtuals-adapter` to public** if you want the reference integration openly visible
   (`gh repo edit AgentWorks-Protocol/virtuals-adapter --visibility public`). It holds no secrets.

*Delete this file once the transfer is done.*
