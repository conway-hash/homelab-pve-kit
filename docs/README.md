# Services

Every service in this repo is optional. The switches live in one file:

```yaml
# ansible/group_vars/all/services.yml
service_enabled:
  kiosk: true
  vault: true
```

Three things read that file, and nothing holds a second copy of it:

| Reader | What it does with it |
|---|---|
| `roles/pve_guests` | creates the guest, or **destroys** it when the flag goes false |
| `ansible/site.yml` | gates the role, or ends the guest's play before it connects |
| `.github/workflows/deploy.yml` | drops switched-off groups from the deploy matrix |

## The services

| Service | Flag | Runs on | Setup |
|---|---|---|---|
| Kiosk dashboard | `kiosk` | the hypervisor | [kiosk.md](kiosk.md) |

Not a service, but it lives here: [updates.md](updates.md) — what patches
itself, what waits for your approval, and what to do when a reboot goes wrong.
| Vaultwarden | `vault` | its own guest, VM 999 | [vault.md](vault.md) |
| Linkwarden | `links` | its own guest, VM 998 | [links.md](links.md) |

The hypervisor itself — `pve_repos`, `tailscale`, `pve_host` — has no switch.
You cannot turn off the machine the rest of this repo runs on, and a flag that
must always be true is not a flag.

## Turning one on

1. Set its flag to `true` in `ansible/group_vars/all/services.yml`
2. Do whatever that service's own doc lists under **Before you turn it on** —
   usually credentials, occasionally a decision
3. Push, or run `ansible-playbook site.yml` locally

A service whose credentials are missing fails with an `assert` naming the
secret and the doc that explains it, before it changes anything on the host.

## Turning one off

Set the flag to `false`. What that means depends on where the service lives:

**A service on its own guest** (`vault`, `links`): the `pve_guests` role
**destroys the VM** on the next run, disk included. Run with `--check --diff`
first if you want to see it coming. Existing archives on `tank` outlive the
guest, but restoring one is a manual job.

Its nightly `vzdump` job is removed with it. That is deliberate: a backup job
pointed at a vmid that no longer exists is accepted by Proxmox and then fails
every night at the appointed hour, which trains you to ignore backup failure
notifications — worse than having none. The same filter means a service that
has never been switched on never gets a job in the first place.

**A service on an existing machine** (`kiosk`): the role stops running, which
means nothing new is installed — but **what a previous run already installed
stays**. Ansible has no undo. Turning the kiosk off stops it being converged;
it does not remove `cage`, `cog`, the systemd units or the autologin stanza
from a box that already had them. Removing those is a manual pass, once,
documented in that service's own file.

That asymmetry is real and worth knowing before you flip something expecting a
clean uninstall. It is not a bug in the switches — declarative tools remove
what they created, and a role that installed a package never "created" it in a
sense it can reverse.

## Adding a service

See **Adding a service** in the [root README](../README.md). The short
version: a role, a play, an inventory group, and a line in `services.yml`
whose name matches the group minus `_host`. CI fails loudly if a play has no
matching flag, so a typo in either file cannot silently produce a service with
no switch.
