# Deploying the TSS signer (production, dedicated VPS)

The **TSS signer** is the only component that holds MPC key-share material. The cloud agent service
(DigitalOcean droplet) and the dashboard (Vercel) hold **no keys** — they build calldata and ask the signer, over the
Cobo relay, to co-sign. This runbook hosts the signer as an always-on daemon on a small VPS.

Why a VPS and not a PaaS build: the signer is a **long-lived stateful daemon** holding a **persistent relay
identity**, not a stateless web service. A `docker run --restart=always` on a locked-down droplet is the
most production-correct and predictable home for it.

## What the signer needs

- **The prebuilt image** `ghcr.io/manuel-dev01/agentworks-tss:latest` (public on GHCR). No source build on
  the host.
- **`tss.env`** — 68 vars that carry each wallet's key share, base64-chunked:
  `TSS_KEYSHARE_{CLIENT,PROVIDER,EVALUATORA,EVALUATORB,EVALUATORC}_B64_*`. The entrypoint reconstructs them
  into `/keys/<name>/` at first boot. **Secret. Never commit it. `chmod 600`.**
- **Outbound network only** to the CAW relay (`wss://ws.caw.tss.cobo.com/ws`). **No inbound ports.**

## Golden rule: one node per relay identity

The CAW relay accepts exactly **one** connected node per wallet identity. Before the hosted signer starts,
**stop every other signer** for these wallets:

- the local `agentworks-signers` container — `docker rm -f agentworks-signers`
- any local Windows `cobo-tss-node` process

If two nodes for the same identity connect, both thrash with `duplicate node ID` errors and signing stalls.

## 1. Provision the droplet

Any provider works; a 2 GB / 1 vCPU instance is plenty (Hetzner CX22 ~€4/mo, DigitalOcean $6/mo).

- Ubuntu 22.04/24.04 LTS.
- **SSH key auth only** — add your public key at creation; do not enable password login.
- Note the public IP.

## 2. Harden (SSH-in as root first)

```bash
# create a non-root sudo user
adduser --disabled-password --gecos "" deploy && usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh && cp ~/.ssh/authorized_keys /home/deploy/.ssh/ \
  && chown -R deploy:deploy /home/deploy/.ssh && chmod 700 /home/deploy/.ssh

# SSH: disable root login + passwords
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/; s/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

# firewall: allow SSH only. The signer needs NO inbound ports (outbound relay only).
apt-get update && apt-get install -y ufw
ufw default deny incoming && ufw default allow outgoing && ufw allow OpenSSH && ufw --force enable

# optional: fail2ban for SSH brute-force protection
apt-get install -y fail2ban && systemctl enable --now fail2ban
```

## 3. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
usermod -aG docker deploy     # let 'deploy' run docker without sudo; re-login to apply
```

## 4. Ship the deploy files (from your workstation)

```bash
scp agents/tss/docker-compose.prod.yml deploy@<VPS_IP>:~/
scp tss.env                            deploy@<VPS_IP>:~/     # your gitignored 68-var file
ssh deploy@<VPS_IP> 'chmod 600 ~/tss.env'
```

`tss.env` is the base64-chunked key-share bundle (produced by `agents/tss/make_keyshare_env.sh`). It is
**secret** — transfer it only over scp, never paste it into a shell, log, or chat.

## 5. Cut over from local → hosted

```bash
# On your workstation: stop the local signer FIRST (frees the relay identities).
docker rm -f agentworks-signers

# On the VPS: pull + start.
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs -f
```

Expect, per profile (`client`, `provider`, `evaluatora`, `evaluatorb`, `evaluatorc`):
`reconstructing key share '<name>' from env` → `starting signer for profile: <name>` → the node connecting
to the relay and staying up (no repeating short-lived exits). The `signer-keys` volume then persists node
state across restarts.

## 6. Verify signing end-to-end

From your workstation (relay identities now served by the VPS):

```bash
cd agents && python scripts/caw_ping.py        # or trigger a small run / a castVote via CAW
```

A successful CAW `contract_call` that returns a real tx hash proves the hosted signer is co-signing. The
authority boundary is unchanged: each wallet still signs only within its scoped Pact.

## Operations

- **Logs:** `docker compose -f docker-compose.prod.yml logs -f` (json-file, rotated 10 MB × 5).
- **Restart:** `docker compose -f docker-compose.prod.yml restart` (retry/backoff in the entrypoint absorbs
  transient relay drops; a healthy node runs >5 min and resets the retry counter).
- **Upgrade image:** `docker compose -f docker-compose.prod.yml pull && … up -d`.
- **Survives reboot:** `restart: always` + Docker's systemd unit bring the signer back automatically.
- **Rotate/refresh key shares:** replace `tss.env`, `docker compose … down` (keep or drop the volume), then
  `up -d`. Dropping `signer-keys` forces a clean reconstruct from env on next boot.

## Current live deployment (DigitalOcean)

The signer runs on a dedicated DO droplet, provisioned with `doctl` + `agents/tss/cloud-init.yml`:

```bash
doctl compute droplet create agentworks-signer \
  --image ubuntu-24-04-x64 --size s-1vcpu-1gb --region fra1 \
  --ssh-keys <key-id> --tag-name agentworks \
  --user-data-file agents/tss/cloud-init.yml --wait
```

- **Droplet:** `agentworks-signer` (fra1, s-1vcpu-1gb). All 5 profiles connected to the relay;
  hosted signature verified on-chain.
- **SSH is on port 443, not 22.** The operator network blocks outbound 22, so `cloud-init.yml` moves sshd
  to 443 (Ubuntu 24.04 is socket-activated, so it disables `ssh.socket` and runs `ssh.service` with
  `Port 22`+`Port 443`). Connect with `ssh -i ~/.ssh/<key> -p 443 root@<ip>` and `scp -P 443 …`.
- `cloud-init.yml` also installs Docker + the compose plugin and locks `ufw` to inbound 22/443 only.
- Verify a hosted signature end-to-end (also useful — replenishes the deployer gas hub):
  `python agents/scripts/verify_hosted_sign.py` → returns a real tx hash, and
  `docker logs agentworks-signer` shows a `SessionTypeSigning … completed` entry.

## Security checklist

- [ ] SSH key-only, root login disabled, password auth off.
- [ ] `ufw` allows **only** OpenSSH inbound; signer exposes no ports.
- [ ] `tss.env` is `chmod 600`, owned by `deploy`, and **not** in git (see `.gitignore`).
- [ ] Exactly one signer per identity online (local stopped before hosted started).
- [ ] Testnet-only wallets (Sepolia); no mainnet key material on the box.
