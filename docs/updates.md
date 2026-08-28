# Keeping the hypervisor up to date

The short version: **security patches take themselves, everything else waits
for you to approve it on the kiosk screen, and nothing reboots on its own.**

That split is deliberate. Automatic security patching is the part with a real
cost to skipping — an unpatched OpenSSL is a hole whether or not you were
paying attention that week. Kernel and Proxmox upgrades are the part where an
unattended machine can come back wrong, and this box is bare metal in your
house with no out-of-band console: if it fails to boot, the fix is walking over
to it. So those wait.

---

## What happens without you

`unattended-upgrades` runs nightly. The `pve_host` role adds one drop-in
(`52-homelab-unattended-upgrades`); Debian's own `50unattended-upgrades`
already allows `Debian-Security` and `Proxmox`, and the drop-in only adds what
that file misses — currently Tailscale's own repo.

| | |
|---|---|
| Taken automatically | Debian security updates, Proxmox security updates, Tailscale |
| Never taken automatically | anything else — Proxmox feature releases, kernels, non-security package updates |
| Reboots | never, automatically |

A kernel *package* can be installed by the nightly run when it ships as a
security update. Installing it does not put it in charge: the running kernel
stays the old one until a reboot, and `/var/run/reboot-required` appears. The
kiosk shows that as a **reboot pending** pill in the header the moment it does.

Proxmox separately mails root every night about pending packages
(`pve_notify_package_updates: always`), delivered locally by postfix. Read it
with `mail`. That is a second, independent notice — it does not install
anything.

---

## What you approve

Everything else shows up in the kiosk's **notifications** panel, split into
three groups that mean genuinely different things:

**"N security updates — taken automatically tonight."** Informational. Do
nothing. They are already going to be installed.

**"N updates waiting for you."** Proxmox packages and non-security updates.
Hold the button for 1.4 seconds and it runs
`apt-get -y -o Dpkg::Options::=--force-confold dist-upgrade`. Config files you
have edited are kept as-is (`--force-confold`) rather than prompting, because
there is nobody at a terminal to answer the prompt.

**"N kernel updates."** Informational — it tells you a reboot is what makes
them real. There is no button, because installing a kernel is part of the
group above.

**"Reboot pending."** Hold the button for 1.4 seconds and the host reboots.
Every guest goes down with it.

All of these are also doable over SSH, and the buttons change nothing about
that:

```bash
ssh ci-deploy@pve.ts.conway-hash.com 'sudo apt-get update && sudo apt-get -s dist-upgrade'
ssh ci-deploy@pve.ts.conway-hash.com 'sudo apt-get -y dist-upgrade'
ssh ci-deploy@pve.ts.conway-hash.com 'sudo reboot'
```

---

## When to do it

**Non-security package updates — whenever you notice them.** Low stakes. If
the kiosk shows a handful and you are at the machine anyway, take them.

**Proxmox point releases (9.2.11 → 9.2.x) — monthly is plenty.** Read
[the Proxmox roadmap](https://pve.proxmox.com/wiki/Roadmap) first if the
version jumps more than a patch number. Services keep running through the
upgrade itself; what changes is that `pveproxy`, `pvedaemon` and friends
restart, so the web UI blinks and the kiosk's guest panel goes briefly empty.
Guests are untouched.

**Kernels — take the update whenever, reboot when it suits you.** A pending
kernel is not urgent unless the changelog says it fixes something you are
exposed to. The realistic cadence here is "next time you are going to be near
the box for other reasons".

**Proxmox major versions (9.x → 10.x) — deliberately, with the release notes
open, never on a whim.** These have a documented upgrade path and can require
repository changes. Not something to do because a number went up.

---

## Before you reboot

The reboot itself is the only routinely disruptive thing in this document, so
it gets a checklist.

1. **Know what goes down.** Every guest. Right now that is `vault` (VM 999) —
   your password manager is unreachable for the duration. If you need a
   password during the reboot, get it out first; the Bitwarden clients cache
   your vault locally and keep working offline, but a fresh login will not.

2. **Check the guests come back on their own.** They are configured to:
   `onboot: 1` with `startup: order=10,up=30`. Nothing to do, but it is the
   thing to check afterwards.

3. **Take a backup if you are about to do something bigger than a kernel.**
   The weekly job runs Saturday 00:00 and keeps the last 4, so the newest
   one can be up to a week old. Before anything risky, take one on demand:

   ```bash
   ssh ci-deploy@pve.ts.conway-hash.com 'sudo vzdump 999 --storage tank --mode snapshot --compress zstd'
   ```

   Backups land on `tank`, a ZFS pool on `sda` — a different physical disk
   from the `sdb` that holds pve-root and every guest disk, so this survives
   that disk dying and not just a mistake.

4. **Reboot**, then confirm:

   ```bash
   ssh ci-deploy@pve.ts.conway-hash.com 'uptime; sudo qm list; systemctl --failed'
   ```

   Or just look at the screen — the kiosk shows all three.

---

## When it goes wrong

**The box does not come back after a reboot.** This is the failure that
justifies the whole manual-approval policy. Proxmox keeps old kernels
installed, so the fix is at the physical console: pick the previous kernel
from the GRUB menu (hold `Shift` during boot if the menu does not appear),
boot it, then pin it while you work out what happened:

```bash
sudo proxmox-boot-tool kernel pin 7.0.14-12-pve
sudo reboot
# and later, once the new one is known good:
sudo proxmox-boot-tool kernel unpin
```

**A guest does not come back.** Check the host first — `systemctl --failed`
and `journalctl -b -u pve-guests` — then the guest's own console with
`qm terminal 999`. The i6300esb watchdog resets a guest that has hard-hung,
so a guest stuck in a boot loop is usually a disk or cloud-init problem
rather than a hang.

**An upgrade broke a service rather than the boot.** Downgrading is
`apt-get install pve-manager=<old-version>`; `apt-cache policy pve-manager`
lists what the repository still offers, and `/var/cache/apt/archives` usually
still holds the `.deb` that was just replaced, which is faster and works even
if the old version has aged out of the repo.

---

## Why unattended-upgrades is not doing more

It could take everything and reboot itself — `Unattended-Upgrade::Automatic-
Reboot "true"` is one line. That was considered and rejected: this host has no
IPMI, no remote console, and a rack of exactly one machine, so an unattended
reboot into a kernel that does not boot is an outage that lasts until you
physically notice. The nightly security stream is the part that genuinely
costs more to skip than to automate. The rest is a button.

If you ever change your mind, the switch is
`unattended_extra_origins` plus a new `Automatic-Reboot` line in
`ansible/roles/pve_host/templates/52-homelab-unattended-upgrades.j2`.
