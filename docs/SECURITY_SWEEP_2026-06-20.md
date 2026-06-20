# PSB Security Sweep + Upgrade Plan — 2026-06-20

## Executive summary
- **No critical hole.** The VPS is **key-only SSH** (`PasswordAuthentication no`), **auto-patched** (`unattended-upgrades active`), secrets are **gitignored + `chmod 600`**, and the dashboard binds **loopback-only** (`127.0.0.1:8082`, reachable only via SSH tunnel). No hardcoded secrets in `src/`, no raw wallet key under the common names.
- The gaps are **defense-in-depth**, not open doors: no network firewall (`ufw inactive`), `PermitRootLogin without-password` (key-only root, but should be `no`), no `fail2ban`, and several **unused API tokens sitting on the box** (blast radius if breached).
- Highest-value upgrade = **network-layer restriction of port 22** (AWS security group to operator IP, or `ufw`) + **trim unused tokens** from the box `.env`.
- VPS = `ubuntu@15.223.198.214` (AWS ca-central, Montreal), systemd `psb-bot`, MemoryMax 1400M. **2GB box → do NOT `apt install` heavy packages** (per standing constraint).

## Findings

### ✅ Already good
| Area | State |
|---|---|
| SSH auth | `PasswordAuthentication no`, `KbdInteractive no`, `PubkeyAuthentication yes` — key-only |
| OS patching | `unattended-upgrades active` |
| Secrets in repo | none hardcoded in `src/`; no 64-hex privkey literals; `.env` gitignored (`*.env` + `!*.env.example`); only `config/secrets.env.example` tracked |
| File perms | `.env` `600`, `~/.ssh/authorized_keys` `600` |
| Dashboard exposure | `127.0.0.1:8082` (tunnel-only, not public) |
| Wallet key | no `PRIVATE_KEY`/`WALLET_PRIVATE_KEY`; Olympus uses API-key auth |
| Brute-force pressure | 267 failed SSH attempts/24h — all fail (key-only); background scan noise only |

### ⚠️ Gaps (ranked)
1. **No network firewall.** `ufw inactive`; port 22 is `0.0.0.0:22` (internet-facing). Key-only auth blocks the realistic attack, but there's no layer in front of sshd and no IP allowlist (memory even noted "lock SSH to op IP" — not done).
2. **Unused tokens on the box** (0 bot-source references): `RAILWAY_TOKEN` (infra/deploy token — worst), `SERPAPI_KEY`, `OBSIDIAN_API_KEY`, `TELEGRAM_BOT_TOKEN`. If the box is breached, these leak for no operational benefit.
3. **`PermitRootLogin without-password`.** Root is key-only (not password), but best practice is `no` (force `ubuntu` + sudo).
4. **No `fail2ban`.** Would cut scan noise + add a layer. Low urgency (key-only makes it cosmetic); **needs apt → defer / weigh against the 2GB no-apt rule.**
5. **`POLYMARKET_PRIVATE_KEY` present in `.env`.** Documented accepted risk (empty/risk-only wallet, no funds → limited blast radius). **Revisit when funding a wallet or migrating to direct Polymarket CLOB** (which needs the raw key for a funded wallet).

## Prioritized action plan

### P0 — highest value, low risk
| # | Action | Who | Risk | Reversible |
|---|---|---|---|---|
| P0.1 | **Restrict port 22 at the AWS security group** to the operator's IP (or a small allowlist). Cleanest option: no lockout risk to the bot, managed in AWS console, no box change. | Operator (AWS console) | Low (don't lock out your own IP) | Edit SG rule back |
| P0.2 | **Remove unused tokens from box `.env`**: `RAILWAY_TOKEN`, `SERPAPI_KEY`, `OBSIDIAN_API_KEY`, `TELEGRAM_BOT_TOKEN`. Verify `MOONSHOT_API_KEY` isn't loaded dynamically by the LLM provider layer before removing it. | Claude (with go) | Low — 0 bot refs; takes effect next restart | Re-add from local `.env` source |

### P1 — defense-in-depth (no apt)
| # | Action | Who | Risk | Reversible |
|---|---|---|---|---|
| P1.1 | If not using the AWS SG (P0.1), enable `ufw` carefully: `sudo ufw allow 22/tcp` **first**, then `sudo ufw --force enable`. (Optionally `allow from <op-ip> to any port 22` instead.) | Operator (lockout risk → do on console/keep a session open) | **Medium — SSH lockout if mis-ordered** | `sudo ufw disable` |
| P1.2 | `PermitRootLogin no` in `/etc/ssh/sshd_config.d/`, then `sudo sshd -t && sudo systemctl reload ssh`. Keep an open SSH session while doing it. | Operator | Medium (sshd misconfig) | Revert file + reload |
| P1.3 | Rotate any token that has ever been committed or shared in plaintext (none found in repo, but rotate `RAILWAY_TOKEN` since infra tokens are high-value). | Operator | Low | n/a |

### P2 — optional / later
| # | Action | Note |
|---|---|---|
| P2.1 | `fail2ban` for sshd | Needs apt; weigh vs 2GB no-apt rule. Low urgency (key-only). |
| P2.2 | Revisit `POLYMARKET_PRIVATE_KEY` on box | Only when funding a wallet / direct CLOB migration. |
| P2.3 | Dependency CVE scan (`pip-audit`) | web3 already removed from hot path (fewer C-ext deps). Run locally, not on box. |

## Explicit DO-NOT
- Do **not** enable `ufw` or edit `sshd_config` blindly from a single remote SSH session — a mis-rule locks everyone out. Use the AWS console path (P0.1) or keep a second session open.
- Do **not** `apt install` on the 2GB box casually (standing constraint; risks OOM/disruption).
- Do **not** expose `8082` publicly to "make the dashboard easier" — keep it loopback + SSH tunnel.
- Do **not** remove `MOONSHOT_API_KEY` (or other LLM provider keys) without confirming the AI client doesn't load them by dynamic name.
