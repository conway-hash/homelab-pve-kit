# Setup

One-time human checklist, in order. Steps 1–2 are done on the Proxmox
host; the rest from your workstation.

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

## 2. Mint a tailnet key (first run only)

The playbook installs Tailscale and joins the tailnet for you — but it
needs a key to join *with*. On the coordination server:

```bash
sudo docker exec headscale headscale users list      # find your numeric ID
sudo docker exec headscale headscale preauthkeys create --user 1
```

⚠️ **Non-ephemeral and single-use.** This is a permanent machine. An
ephemeral key makes the node delete itself the moment it disconnects —
that is what CI's throwaway runner wants (step 5) and the opposite of what
a hypervisor needs. Two keys, two scopes; never reuse one for the other.

```bash
cd ansible
cp group_vars/pve_host/secrets.yml.example group_vars/pve_host/secrets.yml
$EDITOR group_vars/pve_host/secrets.yml     # paste the key
```

A host that's **already** on the tailnet needs none of this — the role
asserts on the key only when a join is actually required, so routine runs
are credential-free. Skip straight to step 3.

## 3. Local run

```bash
cd ansible
cp inventory/hosts.ini.example inventory/hosts.ini
ansible-playbook site.yml --limit pve_host --check --diff   # dry run FIRST
ansible-playbook site.yml --limit pve_host
```

⚠️ **Chicken-and-egg on a stock box:** the inventory addresses the host as
`pve.ts.conway-hash.com`, which does not resolve until Tailscale is
installed and joined — which is what this playbook does. On the very first
run, point it at the LAN address instead:

```bash
ansible-playbook site.yml --limit pve_host -e ansible_host=192.168.1.x
```

Every run after that works over the tailnet name.

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
