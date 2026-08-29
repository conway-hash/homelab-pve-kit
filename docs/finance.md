# Firefly III

Where the money went. Personal finance on its own guest, tailnet only.

```yaml
# ansible/group_vars/all/services.yml
service_enabled:
  finance: true
```

| | |
|---|---|
| Flag | `finance` |
| Runs on | its own guest, VM 997, created by the `pve_guests` role |
| Roles | `common`, `tailscale`, `docker`, `svc_firefly` |
| Settings | `ansible/group_vars/finance_host/vars.yml` |
| Secrets | `ansible/group_vars/finance_host/secrets.yml` (git-ignored) |
| Reachable at | `https://finance.ts.conway-hash.com`, tailnet only |
| Backed up to | `tank`, weekly Sat 00:00, zstd, last 4 kept |

## Why the guest is called `finance` and the role `svc_firefly`

The guest, the tailnet name and the certificate all describe the **job**, so
replacing the software later is not also a rename of a machine, a DNS name and
every bookmark you have. The role names the thing it installs. Same split as
`vault`/`svc_vaultwarden` and `links`/`svc_linkwarden`.

## What it does and does not do

Firefly III tracks accounts, transactions, budgets, bills and categories, and
reports on all of it. It has a rules engine that categorises transactions
automatically as they arrive.

**It does not do OCR.** It cannot read a receipt and create a transaction from
it. It can only *attach* a file to a transaction you already have.

That matters less than it sounds: the **bank feed** is what answers "where does
my money go". An imported transaction already carries date, amount and
merchant, and the rules engine categorises it. A receipt only adds line-item
detail on top. Receipt archiving belongs to Paperless-ngx, and correlating the
two is a job for the assistant, not for Firefly.

## Getting transactions in

Two steps, and the second cannot be done before the first:

1. **This deploy** brings up Firefly itself. Log in, create your account.
2. **The Data Importer** runs beside Firefly on port 8443 and pulls
   transactions in. It authenticates with a Firefly Personal Access Token,
   which cannot exist until someone has logged in and made one — which is why
   it is a separate step rather than part of the first deploy.

### Use an aggregator, not the bank's own API

⚠️ **Not GoCardless.** It closed its free tier to new signups in 2026. Use
**Enable Banking** in *restricted mode*: free, your own accounts only, and no
eIDAS certificate. Direct access to a bank's regulated API needs a qualified
certificate (€2,000–10,000/year) plus TPP registration with the regulator —
which is the entire reason aggregators exist.

⚠️ **Not the bank's commercial API either.** Slovenská sporiteľňa's
"Databanking" is a B2B product for accounting-software vendors and wants a
company and a contract. It is the wrong door.

Whichever aggregator, it is PSD2 *Account Information* only — structurally
incapable of moving money, where a bank or broker key often is not.

Setting it up:

1. Register an application at Enable Banking, **Production**, and save the
   private key it gives you once.
2. Its redirect URL must be
   `https://finance.ts.conway-hash.com:8443/eb-callback` — the port is
   `firefly_importer_port`, and changing one means changing the other.
3. The key goes in as a single base64 line (PEM body, no header/footer/
   newlines). The importer accepts a path or a raw PEM too, but base64 is the
   only form that is both a single-line env value and needs no file whose
   permissions must match whatever uid the container runs as.

⚠️ **PSD2 consent expires every ~90 days.** Every open-banking connection in
Europe does; you re-authorise in the bank's app. Not a Firefly limitation and
not something any provider can waive.

### What it will not cover

Brokers are not banks. **Trading 212 and XTB are not PSD2 institutions**, so
no aggregator reaches them — they have their own read-only APIs instead, and
belong in an assistant tool or a manually-updated asset account rather than in
the bank feed.

---

## Before you turn it on

Four secrets. **Two of them must be exactly 32 characters**, and Firefly fails
quietly at any other length rather than refusing to start:

```bash
openssl rand -base64 24                 # APP_KEY    — exactly 32 chars
openssl rand -hex 16                    # CRON_TOKEN — exactly 32 chars
openssl rand -base64 32 | tr -d '/+='   # POSTGRES_PASSWORD
```

Plus a **separate** Cloudflare API token ("Edit zone DNS" template, one zone),
and a **non-ephemeral** Headscale pre-auth key for the guest's first join.

```bash
gh secret set SVC_FIREFLY_APP_KEY               --body "$(openssl rand -base64 24)"
gh secret set SVC_FIREFLY_CRON_TOKEN            --body "$(openssl rand -hex 16)"
gh secret set SVC_FIREFLY_POSTGRES_PASSWORD     --body "$(openssl rand -base64 32 | tr -d '/+=')"
gh secret set SVC_FIREFLY_CLOUDFLARE_API_TOKEN  # paste
gh secret set SVC_FIREFLY_TS_AUTHKEY            # paste
```

The deploy checks both lengths before it writes anything, because the
alternative is finding out weeks later when a decrypt fails.

## Turning it on

```bash
$EDITOR ansible/group_vars/all/services.yml   # finance: true
git commit -am 'finance: switch on' && git push
```

First registration is open — Firefly gives the **first account** admin and
there is no separate dance for it. Register immediately after the deploy goes
green, before anything else can reach the tailnet.

## The scheduled jobs are not optional

Recurring transactions, bill detection and automated rules only happen when
something calls Firefly's cron endpoint. Nothing calls it by itself, **and
nothing complains when it is never called** — the features simply appear
broken.

Upstream ships an extra Alpine container running `crond`. This uses a systemd
timer instead: one fewer container, and `systemctl list-timers` can answer "is
this actually scheduled" in a way a crond buried in a container cannot. The
deploy's smoke test fails if the timer is not active.

```bash
systemctl list-timers firefly-cron.timer
journalctl -u firefly-cron.service --since -7d
```

The token it authenticates with lives in `cron.sh` at mode 0700, **not** in the
unit's `ExecStart` — unit files are world-readable at 0644, and anyone who can
read it can trigger the job.

## Staying up

Same five layers as the other guests. Two details specific to this stack:

- Firefly **waits on Postgres being healthy**, not merely started
  (`condition: service_healthy`). It runs migrations on boot and exits if the
  database will not answer, so without the gate a cold start is a restart loop
  that settles and looks like nothing happened.
- Its uploads and the Postgres cluster are **named Docker volumes**, not host
  bind mounts. The Postgres server is the one process in these stacks that does
  not run as root; bind-mounting its data directory means pinning its uid, and
  re-asserting ownership on every converge is exactly what broke the links
  guest. A named volume has nothing on the host for Ansible to fight over. They
  are still backed up — `vzdump` images the whole guest disk.

## Turning it off

⚠️ Setting `finance: false` **destroys VM 997 on the next run of the hypervisor
play**, disk and every transaction included, and removes its backup job.
Existing archives on `tank` outlive it. Export from Firefly first if you want
the data in a form you can read without Proxmox.

Retire the credentials too: delete the five GitHub secrets and revoke the
Cloudflare token at source.
