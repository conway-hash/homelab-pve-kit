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

You need a key only when adding a *guest*, which is created fresh and has
to join for the first time:

```bash
sudo docker exec headscale headscale users list      # find your numeric ID
sudo docker exec headscale headscale preauthkeys create --user 1

cd ansible
cp group_vars/pve_host/secrets.yml.example group_vars/<guest>/secrets.yml
$EDITOR group_vars/<guest>/secrets.yml               # paste the key
```

⚠️ **Non-ephemeral and single-use.** A guest is a permanent machine; an
ephemeral key makes it delete itself the moment it disconnects. That is
what CI's throwaway runner wants (step 4) and the opposite of what a real
machine needs. Two keys, two scopes; never reuse one for the other.

## 3. Local run

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

| Secret | What |
|---|---|
| `PVE_SSH_PRIVATE_KEY` | Contents of `~/.ssh/homelab_ci_deploy` |
| `TS_AUTHKEY` | Headscale pre-auth key, **ephemeral + reusable** |

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
