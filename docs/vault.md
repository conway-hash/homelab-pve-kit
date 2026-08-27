# Vaultwarden

A Bitwarden-compatible password manager on its own guest (VM 999), behind its
own Caddy.

```yaml
# ansible/group_vars/all/services.yml
service_enabled:
  vault: true
```

| | |
|---|---|
| Flag | `vault` |
| Runs on | its own guest, VM 999, created by the `pve_guests` role |
| Roles | `common`, `tailscale`, `docker`, `svc_vaultwarden` |
| Settings | `ansible/group_vars/vault_host/vars.yml` |
| Secrets | `ansible/group_vars/vault_host/secrets.yml` (git-ignored) |
| Reachable at | `https://vault.ts.conway-hash.com`, tailnet only |
| Backed up to | `tank` (ZFS on sda), nightly 03:30, zstd, 7 rolling dailies |

## Why a VM and not an LXC container

Tailscale and Docker both need special-casing inside an unprivileged
container, and the usual workaround — running it privileged — removes the
isolation boundary from the one machine on this host that most needs it. A VM
also means the `tailscale` role applies unmodified, exactly as it was written
to.

## Why Caddy lives on that guest

This project ran a shared reverse proxy once and retired it: it bought
prettier URLs at the cost of a wildcard certificate and a DNS-write credential
living on a third box that then had to be maintained forever. A per-service
Caddy has neither problem — the certificate covers one name, the token sits on
the one machine that needs it, and when the guest is down there is nothing
left to proxy.

## Why TLS is not optional here

Every Bitwarden client — browser extension, desktop, mobile — refuses a
non-HTTPS server URL outright. The tailnet already encrypts the transport, but
the clients don't care; they want a certificate. Headscale has no
`tailscale cert` equivalent, so the guest gets its own over ACME DNS-01:

```
vault.ts.conway-hash.com
  ├─ resolves        via Headscale MagicDNS, tailnet members only
  ├─ is validated    by Let's Encrypt over a DNS-01 TXT record in Cloudflare
  └─ is reachable    on the tailnet, and nowhere else
```

"Nowhere else" is enforced by the compose file publishing 443 on this guest's
**tailnet address** rather than `0.0.0.0`. That is not a detail: the guest also
has a LAN address, and a bare `443:443` published the vault to every device on
the home network for as long as it was written that way. Docker's publish rules
also sit in front of the host firewall, so the bind address — not a firewall
rule — is what actually closes it.

That name is simultaneously a MagicDNS name and a real subdomain of a zone in
Cloudflare, which is what makes this cheap: no public DNS record to create, no
port open to the internet, and no HTTP-01 challenge that would have required
one.

---

## Before you turn it on

The hypervisor must already be set up ([SETUP.md](../SETUP.md) steps 1–5), and
because this service brings a guest, so must the Proxmox API token
(SETUP.md step 6).

Then three credentials:

```bash
# 1. Admin token — the Argon2 HASH, not the password. Gates /admin, which
#    can read and rewrite every setting on the instance.
docker run --rm -it vaultwarden/server /vaultwarden hash

# 2. Cloudflare API token, for the ACME DNS-01 challenge.
#    Use the "Edit zone DNS" TEMPLATE, restricted to the ONE zone.
#    That grants Zone:Read + DNS:Edit. Both are needed: the plugin walks
#    up from _acme-challenge.vault.ts.conway-hash.com to find which zone
#    owns it, and a token with DNS:Edit alone cannot do that lookup.
#    Created at https://dash.cloudflare.com/profile/api-tokens

# 3. A NON-ephemeral Headscale pre-auth key for the guest itself.
sudo docker exec headscale headscale preauthkeys create --user 1
```

⚠️ That third key is **not** the same as CI's `TS_AUTHKEY`. The runner's is
ephemeral so a throwaway node reaps itself; this one is non-ephemeral because
an ephemeral key makes a permanent machine delete itself the moment it
disconnects. Two keys, two scopes; never reuse one for the other.

There is no DNS record to create — see the diagram above.

### For a local run

```bash
cd ansible
cp group_vars/vault_host/secrets.yml.example group_vars/vault_host/secrets.yml
$EDITOR group_vars/vault_host/secrets.yml
```

### For CI

Every service secret is named `SVC_<SERVICE>_<THING>`, matching the
`svc_<service>_` prefix on its Ansible variable. Two things fall out of that:
you can tell at a glance which secrets belong to a service (and delete exactly
those when you retire it), and the next service wanting a Caddy gets its **own**
Cloudflare token rather than a share of this one. Same zone, same permissions —
but revoking one does not take the others down with it, and the audit log says
which service did what.

Secrets without that prefix (`PVE_*`, `TS_AUTHKEY`) are infrastructure and
outlive any single service.

| Secret | What |
|---|---|
| `SVC_VAULTWARDEN_ADMIN_TOKEN` | the Argon2 hash from step 1 |
| `SVC_VAULTWARDEN_CLOUDFLARE_API_TOKEN` | the token from step 2 |
| `SVC_VAULTWARDEN_TS_AUTHKEY` | the key from step 3 — first run only; once joined, the role skips it |

## Turning it on

```bash
$EDITOR ansible/group_vars/all/services.yml   # vault: true

cd ansible
# Creates the VM. The guest plays below cannot run before this one has.
ansible-playbook site.yml --limit pve_host
ansible-playbook site.yml --limit vault_host
```

The first run is slow: it compiles Caddy with the Cloudflare DNS plugin, then
waits on an ACME issuance. The playbook's last task polls
`https://vault.ts.conway-hash.com/alive` for up to five minutes and fails if it
never answers.

## Then, before you put anything in it

```bash
# Registration is closed by default — confirm it, from another machine.
curl -s https://vault.ts.conway-hash.com/api/config | jq .

# Create your own account by invitation, from the admin page:
#   https://vault.ts.conway-hash.com/admin
```

⚠️ Check the backup actually ran before you trust the vault with anything.
`pve_host` creates a nightly `vzdump` job, but a job that has never succeeded
is not a backup:

```bash
ssh ci-deploy@pve.ts.conway-hash.com 'sudo ls -la /tank/dump/'
```

⚠️ This host also carries a hand-made, all-guests backup job (02:30 → `tank`,
`keep-last 7`) that predates this repo and is **not** managed by it — it is
what covers the unmanaged VM 100. It backs the vault up a second time, which
costs ~1.8G a night and a second snapshot freeze but is otherwise harmless. To
stop that, exclude the vault from it by hand; there is deliberately no Ansible
task for this, because the repo does not own jobs it did not create:

```bash
ssh ci-deploy@pve.ts.conway-hash.com \
  'sudo pvesh set /cluster/backup/backup-2731efbf-8ff9 --exclude=999'
```

Archives are `.vma.zst` (~1.8G). If you ever see a bare `.vma` (~4.9G), the
job lost its `--compress` — see `pve_backup_compress` in
`group_vars/pve_host/vars.yml` for why that matters more than it looks.

## Staying up

Five failure modes, handled in five different places:

| What broke | What handles it |
|---|---|
| The host was powered off and on | `onboot` + a startup order, set by `pve_guests` |
| The guest rebooted | `restart: unless-stopped`, plus a systemd unit for the stack |
| A container's process exited | Docker's restart policy |
| The stack is up but not answering | a systemd timer that curls `/alive` and restarts after 3 consecutive misses |
| The guest kernel panicked or hung | the emulated i6300esb watchdog, petted from inside the guest |

The fourth row exists because Docker's restart policy reads **exit codes, not
healthchecks** — an unhealthy container is restarted by nothing. A wedged
Vaultwarden, or a Caddy sitting on a certificate it failed to renew, would
otherwise stay "up" indefinitely with no client able to reach it. A
`healthcheck:` block in the compose file would have been decorative.

The honest gap: **single-node Proxmox will not restart a crashed VM.** That is
the HA manager's job and HA needs a cluster with quorum. Standing up corosync
on one node to protect one guest adds more that can break than it covers, so
the hardware watchdog covers the realistic case instead.

## Turning it off

⚠️ Setting `vault: false` **destroys VM 999 on the next run of the hypervisor
play**, disk and vault contents included. Run `--check --diff` first if you
want to see it coming.

Backups from the `vzdump` job outlive the guest, but restoring one is a manual
job. If you want the data, export your vault from a Bitwarden client first —
that is the only copy in a format you can read without Proxmox.

Retire the credentials too, rather than leaving them sitting unused: delete the
three GitHub secrets, and revoke the Cloudflare token at its source — it is a
DNS-write credential for your whole zone and nothing else in this repo uses it.

## Troubleshooting

**No certificate.** Almost always the Cloudflare token's scope. It needs
the "Edit zone DNS" template (Zone:Read **and** DNS:Edit) on the zone that
owns `conway-hash.com`. A token with DNS:Edit but no Zone:Read cannot resolve
which zone the name belongs to and fails before it ever writes a record;
a token scoped to the wrong zone fails the challenge outright. Either way the
error is buried in Caddy's log:

```bash
ssh ci-deploy@vault.ts.conway-hash.com 'sudo docker logs vaultwarden-caddy 2>&1 | tail -50'
```

**The name doesn't resolve.** MagicDNS only answers for tailnet members. Check
the client is actually on the tailnet, and that the guest joined:

```bash
ssh ci-deploy@pve.ts.conway-hash.com 'sudo tailscale status | grep vault'
```

**It answers, then stops, then answers again.** That is the self-heal timer
doing its job — and a sign something is wrong underneath it:

```bash
ssh ci-deploy@vault.ts.conway-hash.com 'journalctl -u vaultwarden-healthcheck.service --since -1d'
```
