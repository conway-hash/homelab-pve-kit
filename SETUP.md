# Setup

## What must already be true

This playbook takes over from a **stock Proxmox VE install that is already
on the tailnet**. Four things it cannot do for itself — everything else it
fixes.

### 1. Proxmox VE 9 or newer (Debian 13 trixie)

Installed from the ISO, nothing else done to it.

Apt has two formats for "where do I download packages from": the old
one-line `.list` files, and the newer multi-line `.sources` files. **PVE 9
uses `.sources`; PVE 8 and older use `.list`.** This repo manages
`.sources` only, so on PVE 8 it would write a file the system never reads
— reporting success while changing nothing. `pve_repos` checks the Debian
major version and stops with an explicit message rather than doing that.
Check yours with `pveversion`.

### 2. Tailscale installed and joined

Ansible reaches these machines over the tailnet and nowhere else — they
have no public address. So the host must already answer to
`pve.ts.conway-hash.com` before anything here runs.

```bash
tailscale up --login-server=https://vpn.conway-hash.com --authkey=<key>
```

Use a **non-ephemeral** key: this is a permanent machine, and an ephemeral
one deletes the node the moment it disconnects.

(The `tailscale` role still installs and joins — that's the path for
*guests*, which are created fresh. On the hypervisor it detects an
existing join and does nothing.)

### 3. `sudo` installed

```bash
apt install sudo
```

⚠️ The playbook **cannot** do this one for you, though it does keep sudo
installed afterwards. Ansible's `become:` needs sudo to already exist in
order to run any task at all — including the task that would install it.

This works even though the enterprise repo is 401ing on a fresh box: only
that one repo fails, the Debian repos are fine, and `sudo` comes from
Debian.

### 4. A `ci-deploy` account with a key and sudo rights

Step 1 below.

---

## 1. Create the two accounts



On the Proxmox host, as root:

```bash
apt update && apt install -y sudo    # Proxmox does not ship sudo

# read-only account, for inspection
useradd -m -s /bin/bash claude
install -d -m 700 -o claude -g claude /home/claude/.ssh
# paste the claude public key into /home/claude/.ssh/authorized_keys
chmod 600 /home/claude/.ssh/authorized_keys
chown claude:claude /home/claude/.ssh/authorized_keys
usermod -aG adm claude               # read logs without sudo
# NO sudoers entry for claude — deliberate

# deploy account, for CI
useradd -m -s /bin/bash ci-deploy
install -d -m 700 -o ci-deploy -g ci-deploy /home/ci-deploy/.ssh
# paste the ci-deploy public key into /home/ci-deploy/.ssh/authorized_keys
chmod 600 /home/ci-deploy/.ssh/authorized_keys
chown ci-deploy:ci-deploy /home/ci-deploy/.ssh/authorized_keys

echo 'ci-deploy ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ci-deploy
chmod 440 /etc/sudoers.d/ci-deploy
visudo -c                            # must print "parsed OK" before you log out
```

⚠️ `apt install sudo` comes **first**. Without it `visudo` doesn't exist,
the script dies mid-way, and `/etc/sudoers.d/ci-deploy` sits there inert.

Keypairs, on your workstation:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/homelab_claude    -N '' -C "claude read-only"
ssh-keygen -t ed25519 -f ~/.ssh/homelab_ci_deploy -N '' -C "homelab-pve ci-deploy"
```

Passphrase-less on purpose — Ansible and CI both need them
non-interactively.

## 2. Tailnet key — guests only, skip for the hypervisor

The Proxmox host is already on the tailnet (prerequisite 2), so the
`tailscale` role detects the existing join and does nothing. **Nothing in
step 3 needs a secret.**

You need a key only when a service brings a *guest*, which is created fresh
and has to join for the first time. Which services those are, and where each
one wants its key, is in that service's own doc — this is just how the key is
minted:

```bash
sudo docker exec headscale headscale users list      # find your numeric ID
sudo docker exec headscale headscale preauthkeys create --user 1

cd ansible
cp group_vars/<group>/secrets.yml.example group_vars/<group>/secrets.yml
$EDITOR group_vars/<group>/secrets.yml               # paste the key
```

⚠️ **Non-ephemeral and single-use.** A guest is a permanent machine; an
ephemeral key makes it delete itself the moment it disconnects. That is
what CI's throwaway runner wants (step 4) and the opposite of what a real
machine needs. Two keys, two scopes; never reuse one for the other.

## 3. Local run — the hypervisor

```bash
cd ansible
cp inventory/hosts.ini.example inventory/hosts.ini
ansible-playbook site.yml --limit pve_host --check --diff   # dry run FIRST
ansible-playbook site.yml --limit pve_host
```

The inventory addresses the host by its tailnet name, which is why
Tailscale is a prerequisite rather than something this playbook bootstraps
on the hypervisor.

⚠️ Always `--check --diff` first against a live hypervisor. It shows
exactly what would change before anything is touched.

## 4. GitHub secrets, for CI deploys

| Secret | What | Needed by |
|---|---|---|
| `PVE_SSH_PRIVATE_KEY` | Contents of `~/.ssh/homelab_ci_deploy` | everything |
| `TS_AUTHKEY` | Headscale pre-auth key, **ephemeral + reusable** | everything |
| `PVE_API_TOKEN_SECRET` | Secret half of the Proxmox API token (step 6) | creating guests |

Services bring their own secrets on top of these, named
`SVC_<SERVICE>_<THING>` so you can tell at a glance which belong to what — and
delete exactly those when you retire a service. They are listed in each
service's own doc rather than here — a service you never turn on should not leave you
wondering which of these rows you skipped. See
[docs/README.md](docs/README.md).

⚠️ Where a service needs a tailnet key of its own, it is **not** `TS_AUTHKEY`.
The runner's is ephemeral so a throwaway node reaps itself; a permanent
machine's must be non-ephemeral, because an ephemeral key makes it delete
itself the moment it disconnects. Two keys, two scopes; never reuse one for
the other.

```bash
gh secret set PVE_SSH_PRIVATE_KEY < ~/.ssh/homelab_ci_deploy
```

The tailnet key must be ephemeral and reusable — the runner is a
throwaway node that should clean itself up. That is a **different scope**
from the non-ephemeral key a permanent machine joins with; don't reuse one
for the other.

If you want the account separation to actually hold, `shred -u
~/.ssh/homelab_ci_deploy` afterwards so the privileged key exists only in
GitHub Secrets. Otherwise it sits on your workstation and the boundary is
a guardrail against mistakes rather than a wall.

## 5. Check notifications work

```bash
ssh ci-deploy@pve 'sudo pvesh get /cluster/options --output-format json' | jq .notify
# expect: package-updates=always

ssh ci-deploy@pve 'sudo mail'          # nightly reports land here
```

Also visible with zero config at **Node → Updates** in the web UI.

## 6. A Proxmox API token — only if you are creating guests

Skip this entirely if you only run the hypervisor itself. Guests are created
through the Proxmox API, which needs its own token — not the root password,
and not shared with anything else.

On the host, as root:

```bash
pveum role add Provisioner -privs \
  "Datastore.Allocate Datastore.AllocateSpace Datastore.Audit \
   Pool.Allocate Sys.Audit Sys.Modify \
   VM.Allocate VM.Audit VM.Backup VM.Clone \
   VM.Config.CDROM VM.Config.Cloudinit VM.Config.CPU VM.Config.Disk \
   VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options \
   VM.GuestAgent.Audit VM.Migrate VM.PowerMgmt"

pveum user add automation@pve
pveum aclmod / -user automation@pve -role Provisioner
pveum user token add automation@pve homelab --privsep 0
```

⚠️ `--privsep 0` matters. With privilege separation **on**, the token gets its
own empty ACL rather than inheriting the user's, so every call returns 403 and
the error says nothing about why.

The secret is shown **once**. Only that secret half is a secret — the user
(`automation@pve`) and token ID (`homelab`) are ordinary values and already
live in `group_vars/pve_host/vars.yml`. Set the secret as
`PVE_API_TOKEN_SECRET`.

`Sys.Modify` is in there for the guest's network device, and `VM.Config.HWType`
covers the watchdog. Dropping either produces a 403 partway through, with a
half-built VM.

⚠️ **PVE 9 removed `VM.Monitor`** — it was replaced by the `VM.GuestAgent.*`
family. Older guides still list it, and `pveum role add` rejects the whole
command if any single privilege is invalid, leaving no role behind while the
`pveum user add` that follows still succeeds. If you hit that, the fix is to
re-run the role and `aclmod` lines only; the user and token are already there.

To see the valid set on your own host:

```bash
grep -oE "VM\.[A-Za-z.]+" /usr/share/perl5/PVE/AccessControl.pm | sort -u
```

There is nothing else to set up — no state file, no bucket, no cloud account.
That was the point of dropping OpenTofu.

## 7. Turn on the services you want

Everything above is the host. Services are separate, optional, and each has
its own file:

```yaml
# ansible/group_vars/all/services.yml
service_enabled:
  kiosk: true
  vault: true
```

| Service | Flag | Setup |
|---|---|---|
| Vaultwarden | `vault` | [docs/vault.md](docs/vault.md) |
| Kiosk dashboard | `kiosk` | [docs/kiosk.md](docs/kiosk.md) |

Each doc lists what that service needs **before** you turn it on — its
credentials, its GitHub secrets, and how to check it actually works — plus
what turning it off does, which is not the same for a service that owns a
guest as for one that doesn't. Start at [docs/README.md](docs/README.md).

Nothing here needs editing to skip a service. Leave its flag `false` and
Ansible will not create its guest, will not converge it, and CI will
not deploy to it.

## Later: real push notifications

Local root mail is a mailbox nobody opens. Once ntfy or Gotify runs on a
guest, add a webhook endpoint and a matcher to `roles/pve_host/`:

```bash
pvesh create /cluster/notifications/endpoints/gotify --name ... --server ... --token ...
pvesh create /cluster/notifications/matchers --name ... \
  --match-field=exact:type=package-updates --target=...
```

An endpoint with no matcher pointing at it receives nothing — the single
most common reason people configure PVE notifications and hear nothing
back.
