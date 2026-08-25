# homelab-pve-kit

Ansible for the Proxmox VE host and every guest running on it.

The hypervisor is bare metal, installed from the ISO — nothing here creates
it. This repo **converges a machine that already exists**, and creates the
guests on top of it.

Ansible-only, deliberately. OpenTofu was used for the guests briefly and
dropped: a VM on a single node is flat parameters with no resource graph to
model, and the only thing OpenTofu actually required was somewhere to keep a
state file — which meant either a cloud bucket or a hand-managed service on
the hypervisor, in a repo whose stated point is needing neither. Ansible asks
Proxmox what exists rather than remembering, which also suits a box you will
click around in by hand. What that costs is `plan`: see
[docs/README.md](docs/README.md).

Companion repo: [homelab-vpn-kit](https://github.com/conway-hash/homelab-vpn-kit)
— the Headscale coordination server on GCP. Deliberately separate: this
repo is useful to anyone with a Proxmox box, with or without Headscale.
The two tailnet facts they share are declared once here and checked
against upstream in CI.

## What it does

On the hypervisor, in this order — which matters on a box that has never
been touched:

| Role | What |
|---|---|
| `pve_repos` | Disables the enterprise + Ceph repos, enables `pve-no-subscription`. **First**, because out of the box the enterprise repo is on with no subscription and every `apt` call 401s until this runs. |
| `tailscale` | Installs and joins the tailnet. Not Proxmox-specific — guests reuse it as-is. |
| `pve_host` | `sudo` (Proxmox doesn't ship it), unattended upgrades with **no automatic reboot**, Tailscale added to the allowed origins, and `package-updates=always`. |
| `pve_kiosk` | Drives the monitor physically attached to the box: host metrics, guests, pending updates, and a live tailnet graph. |

And on each guest, in this order:

| Role | What |
|---|---|
| `common` | The baseline every guest gets: qemu-guest-agent, a hardware watchdog, unattended upgrades. **First**, because everything after it is easier to recover from once the machine can reset itself when it wedges. |
| `tailscale` | The same role the hypervisor runs, unmodified. |
| `docker` | Engine + Compose plugin from Docker's own repo, not Debian's `docker.io`. |
| `svc_<name>` | The service. Self-contained. |

## Services

Every service is optional, and every switch lives in one file:

```yaml
# ansible/group_vars/all/services.yml
service_enabled:
  kiosk: true
  vault: true
```

| Service | Flag | Runs on | Docs |
|---|---|---|---|
| Vaultwarden | `vault` | its own guest, VM 101 | [docs/vault.md](docs/vault.md) |
| Kiosk dashboard | `kiosk` | the hypervisor | [docs/kiosk.md](docs/kiosk.md) |

Three things read that file and none of them holds a second copy of it:
`roles/pve_guests` creates or destroys the guest, `ansible/site.yml` gates the
role — or ends the guest's play before it connects — and `deploy.yml` drops
switched-off groups from its matrix. Turning something on is a flag plus
whatever that service's own doc lists under **Before you turn it on**.

The hypervisor itself has no switch. You cannot turn off the machine the rest
of this repo runs on, and a flag that must always be true is not a flag.

See [docs/README.md](docs/README.md) for what turning a service *off* actually
does — it differs depending on whether the service owns a guest, and only one
of the two cases is a clean removal.


## What it assumes already exists

A Proxmox VE 9 box that is **already on the tailnet**, with `sudo`
installed and a `ci-deploy` account. Those four things can't bootstrap
themselves — Ansible connects over the tailnet and escalates with sudo, so
both have to exist before it can run at all. `SETUP.md` covers each one and
why.

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

No state file, no backend, no cloud account. The only thing this repo needs
beyond the Proxmox box itself is a tailnet to reach it over.

## Deliberately not here

Decided, not overlooked — don't re-propose these without a new reason.

**Container CVE scanning (Trivy, Grype, Docker Scout).** Skipped
2026-08-23 on the grounds that there was nothing to scan. That reason has
now expired: `svc_vaultwarden` brings two pinned images, one of them built
here. Still not added, but the argument is different now and weaker — this
is the next thing to revisit, not a settled no.

If it is added: it is a separate concern from Renovate, which answers "is
there a newer version" where a scanner answers "does what I run have known
holes"; neither substitutes for the other. And it has to run on a
**schedule**, because a CVE is published against an image you already run
without you committing anything, so a push-triggered scan would never
fire.

**Dependabot.** Replaced by Renovate in `homelab-vpn-kit` (commit
`60e3aad`) and not reintroduced here. Dependabot cannot follow a version
pinned as an Ansible variable; Renovate's `customManagers` can. Running
both just means two bots opening the same GitHub Actions PR. Dependabot
*security* alerts stay on — they need no config and don't collide.

## Layout

```
docs/
├── README.md                 the switches, and what off actually means
├── vault.md                  one file per service — setup, checks, teardown
└── kiosk.md

ansible/
├── site.yml                  one play per host group
├── group_vars/
│   ├── all/
│   │   ├── services.yml      ← every service's on/off switch
│   │   └── vars.yml          tailnet facts (drift-checked against upstream)
│   │                         + the guest baseline
│   ├── pve_host/vars.yml
│   └── vault_host/vars.yml
└── roles/
    ├── pve_repos/ pve_host/ pve_guests/   the hypervisor, and the VMs it hosts
    ├── pve_kiosk/                         a service, on the hypervisor
    ├── tailscale/ common/ docker/         any machine
    └── svc_<name>/                        one self-contained role per service
```

Groups are named `<thing>_host` and machines `<thing>` — `pve_host`/`pve`,
`vault_host`/`vault`. A group and a host sharing one name makes `--limit`
ambiguous and Ansible only warns about it.

## Adding a service

**To an existing guest**, six steps, and none of them touch a workflow
file — CI derives its matrix from `site.yml`'s own `hosts:` lines crossed
with the switches:

1. `roles/svc_<name>/` — self-contained: its own tasks, templates, and a
   `defaults/main.yml` holding only secrets, each with an `assert`
2. A group in `inventory/hosts.ini`, named `<name>_host`
3. A play in `site.yml`, gated on the flag
4. A line in `group_vars/all/services.yml` — the name must match the group
   minus `_host`. CI fails loudly if a play has no matching flag, so a typo
   in either file cannot silently produce a service with no switch.
5. `docs/<name>.md`, and a row in `docs/README.md`
6. A `renovate:` comment **on the line immediately above** each version
   var in that group's `vars.yml` — the `customManagers` entry in
   `renovate.json` already matches every `group_vars/*/vars.yml`, so no
   config change is needed. Anything in the gap between the comment and
   the var makes Renovate stop tracking it silently: no error, just a
   dependency that quietly never updates again.

**On a new guest**, add an entry to `pve_guests:` in
`group_vars/pve_host/vars.yml` — name, vmid, cores, RAM, disk, address. It
gets cloned from the cloud-init template the role builds once, so there is no
image to fetch and no disk to import per service. That needs the Proxmox API
token to exist (SETUP.md step 6); the workflows stay untouched.

The one thing a new guest DOES require in a workflow: **its own smoke
test** in `deploy.yml`. A deploy job whose last step is `ansible-playbook`
has proven only that Ansible reported ok — this repo has shipped a
false-green run that way before.

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
