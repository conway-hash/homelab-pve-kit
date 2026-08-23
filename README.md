# homelab-pve-kit

Ansible for the Proxmox VE host and the guests running on it.

The hypervisor is bare metal, installed from the ISO — nothing here
creates it. This repo **converges a machine that already exists**, which
is why it's Ansible-only and has no OpenTofu: Terraform manages resources
it created, and it never created this box. `terraform/` arrives the day
the first *new* VM does, at which point its state starts empty and no
import is needed.

Companion repo: [homelab-vpn-kit](https://github.com/conway-hash/homelab-vpn-kit)
— the Headscale coordination server on GCP. Deliberately separate: this
repo is useful to anyone with a Proxmox box, with or without Headscale.
The two tailnet facts they share are declared once here and checked
against upstream in CI.

## What it does

Three roles, applied in this order — which matters on a box that has
never been touched:

| Role | What |
|---|---|
| `pve_repos` | Disables the enterprise + Ceph repos, enables `pve-no-subscription`. **First**, because out of the box the enterprise repo is on with no subscription and every `apt` call 401s until this runs. |
| `tailscale` | Installs and joins the tailnet. Not Proxmox-specific — guests reuse it as-is. |
| `pve_host` | `sudo` (Proxmox doesn't ship it), unattended upgrades with **no automatic reboot**, Tailscale added to the allowed origins, and `package-updates=always`. |

Written to converge a **stock Proxmox install**, not just one that's
already been set up by hand. Everything is idempotent — a second run is
`changed=0`.

## Reading update notifications

Proxmox has **no in-UI notification inbox**. Two places to look:

- **Node → Updates** in the web UI — lists pending packages, no config
- **`mail` on the host** — nightly reports from `pve-daily-update`

Real push delivery (ntfy or Gotify) needs a service to point at. When one
exists, it's a webhook endpoint plus a matcher in `roles/pve_host/` — the
rest of the design already accounts for it.

## Scope

The hypervisor, plus guests **created by this repo**. VM 100 (Obsidian
sync + DB) predates it, stays on the LAN, and is not managed here.

## Layout

```
ansible/
├── site.yml                  one play per host group
├── group_vars/
│   ├── all/vars.yml          tailnet facts (drift-checked against upstream)
│   └── pve_host/vars.yml
└── roles/
    ├── pve_host/             the hypervisor
    ├── common/               baseline every guest gets   (not built yet)
    └── svc_<name>/           one self-contained role per service
```

## Adding a service

Four steps, and **none of them touch a workflow file** — CI derives its
matrix from `site.yml`'s own `hosts:` lines.

1. `roles/svc_<name>/` — self-contained: its own tasks, templates, and a
   `defaults/main.yml` holding only secrets, each with an `assert`
2. A group in `inventory/hosts.ini`
3. A play in `site.yml`
4. A `customManagers` entry in `renovate.json` if it pins a version

## Two accounts, on purpose

| Account | Sudo | For |
|---|---|---|
| `claude` | none | Read-only inspection |
| `ci-deploy` | `NOPASSWD:ALL` | The only account that changes anything |

Restricted-sudo whitelists were considered and rejected: `sudo cat` reads
`/etc/shadow`, `sudo journalctl` shells out through its pager. No-sudo is
a real boundary; a whitelist is a comfortable-feeling fake one.

## Using this as a template

Values in `group_vars/` are real and live, kept as worked examples rather
than placeholders. Every line that needs changing is marked `<-- change
this`. Start with `SETUP.md`.

## License

MIT — see [LICENSE](LICENSE).
