# Kiosk dashboard

Drives the monitor physically attached to the Proxmox box: host metrics with
history, guests and the containers inside them, a live tailnet graph, the
Proxmox task log, and the buttons that approve an upgrade or a reboot.

```yaml
# ansible/group_vars/all/services.yml
service_enabled:
  kiosk: true
```

| | |
|---|---|
| Flag | `kiosk` |
| Runs on | the hypervisor — there is no guest |
| Role | `pve_kiosk` |
| Settings | `ansible/group_vars/pve_host/vars.yml` |
| Secrets | none |
| Reachable at | the attached screen, and `127.0.0.1:8099` on the host |

## Why it runs on the hypervisor

The screen is wired to that machine, so the compositor and browser have to run
there — a guest can't reach it without GPU passthrough, which would hand the
card to the VM and take the host console with it. The data is all local
anyway, so nothing is gained by moving it off.

```
kiosk.service     → kioskd.py: samples, keeps history, serves 127.0.0.1:8099
tty1 (autologin)  → cage -- cog http://127.0.0.1:8099/   (|| btop)
kiosk-lofi.timer  → lofi.sh at 20:00, stopped by kiosk-lofi-stop.timer at 03:00
```

Nothing is served off-box: the daemon binds `127.0.0.1` only, so there is no
port to firewall.

`cog`, not chromium: Debian's chromium pulls 174 packages onto a hypervisor,
and even with recommends disabled still installs `cups-common`,
`system-config-printer` and `upower`. `cog` is WebKit built for kiosks and adds
no daemons.

If cage or cog fails to start, tty1 falls through to `btop`, so the screen
never goes blank.

---

## One daemon, three clocks

`kioskd.py` replaced a bash `collect.sh` on a 10s systemd timer plus
`python3 -m http.server`. A timer has exactly one interval, so everything ran
at the speed of the slowest source — `/proc/stat` is free and `pvesh` costs
~150ms, so "cheap enough for 1s" got dragged down to 10s and every graph was a
staircase. History also lived in the browser, which meant a reload started the
lines over.

| Tier | Every | Reads |
|---|---|---|
| fast | `kiosk_fast_seconds` (1s) | `/proc/stat` per core, `/proc/meminfo`, `/proc/net/dev`, `k10temp`, `amdgpu` sysfs |
| guest | `kiosk_guest_seconds` (5s) | `pvesh` guest list, and `status/current` per running guest |
| agent | `kiosk_agent_seconds` (20s) | `qm guest exec … docker ps` and `qm agent … get-fsinfo` inside each guest |
| slow | `kiosk_slow_seconds` (60s) | tailscale, HTTPS probes, `apt-get -s dist-upgrade`, failed units, PVE tasks, journal |

History is a ring buffer in the daemon's memory — `kiosk_history_points`
samples per series, 600 by default, which at a 1s tick is a 10-minute window.
Nothing is written to disk, so restarting the daemon starts the graphs over.
The page asks for everything after the sequence number it last saw, so a 1Hz
poll ships one number per series rather than the whole window.

Still stdlib-only Python. Nothing was installed to make this work.

## The guest memory numbers are the balloon's, not QEMU's

This is worth knowing because the old dashboard got it wrong and the wrong
number is very believable.

`pvesh get /nodes/pve/qemu` returns a `mem` field that is the QEMU **process**
RSS on the host. For `vault` that reads about 2.05 GiB against a 2 GiB
assignment — over 100% — because it counts the emulator's own overhead
alongside the guest's RAM. It is not a guest metric at all.

`/status/current` additionally carries `ballooninfo`, filled in by the virtio
balloon driver **inside** the guest. `total_mem - free_mem` is the number the
guest itself would report, and the number Proxmox's own UI draws. For the same
VM that is about 1.63 GiB of 1.93 GiB, or 85%.

The kiosk reads the second one. Where a guest has no balloon driver it falls
back to the host figure and says so in amber on the card, rather than quietly
showing you the wrong one.

## What the tailnet graph is showing

Nodes are laid out by a small spring simulation — settled once per update and
then left alone, because a hypervisor should not run a physics loop at 60fps.
Labels sit on opaque plates below each node instead of over the edges, which
is what made the previous version unreadable.

Each node shows the name Headscale knows it by, its tailnet address, and how
this host reaches it: **direct**, **via `<relay>`**, or **offline**. There is
no relay node drawn in the middle — a relay is a property of the link, not a
machine on your tailnet.

A dashed box groups this hypervisor with its own guests. That relationship is
inferred by matching tailnet hostnames against the guest list, because
Headscale has no notion of one node hosting another — the box is the only
thing that can observe it.

`TLS` next to a node means a TCP connection to port 443 on its MagicDNS name
succeeded; green if the certificate verified, amber if it did not. No request
is ever sent, so nothing shows up in the far end's access log. Days to expiry
are along the bottom, and anything under 21 days becomes a notification.

**Relayed peers are deliberately not notifications.** A relayed link is slower,
not broken, and a permanent line on a list headed "notifications" is how you
learn to stop reading the list. It is on the graph, where it belongs.

## Guests, containers, and the tabs

The middle column is tabbed by where a guest came from:

| Tab | What lands there |
|---|---|
| automated | vmid appears in `pve_guests` — this repo created it and owns it |
| templates | `template: 1` in Proxmox |
| manual | everything else, i.e. someone clicked it together |

That classification falls straight out of the repo's own vmid allocation rule
(machines this repo creates count down from 999, hand-made ones live in the
100s), so nothing has to be told which guests Ansible owns.

The default tab is **automated**. With nobody touching the keyboard for
`kiosk_idle_seconds`, the view rotates between tabs that actually have
something in them every `kiosk_tab_seconds`. Touch the mouse or keyboard and
rotation stops until you go quiet again. An empty tab is faded, still shows its
count, and says "no machines in this group" if you select it.

Keyboard: `1`/`2`/`3` pick a tab, `←`/`→` step through them, `r` forces a
refresh.

Container lists come through the QEMU guest agent — `qm guest exec <vmid> --
docker ps`. No SSH, no key, and no listening port in the guest: the channel is
a virtio serial device, so it works on a guest that has fallen off the network
entirely. It does run as root inside the guest, which is why the only things
sent through it are read-only listings.

## The approve buttons

The notifications panel grows buttons for the update classes that are
deliberately **not** automated — see [updates.md](updates.md) for the policy
they implement. They are hold-to-confirm for 1.4 seconds, because the
realistic accident on a machine with a keyboard in front of it is a sleeve on
the mouse.

The security model is the bind address. `POST /api/do` is on `127.0.0.1`, so
reaching it means being root on this box already or standing in front of the
monitor — the same bar as the power button, which does considerably more
damage. It also requires an `X-Kiosk: 1` header, which a cross-origin form
post cannot set.

---

## The lofi stream

Off unless `kiosk_lofi_url` is set. With it set, `mpv` plays the stream
audio-only at `kiosk_lofi_start`, from silence, climbing to
`kiosk_lofi_volume` over `kiosk_lofi_ramp_minutes`, and stops at
`kiosk_lofi_stop`. The header pill toggles it by hand.

```yaml
# ansible/group_vars/pve_host/vars.yml
kiosk_lofi_url: "https://www.youtube.com/watch?v=..."
kiosk_lofi_volume: 45
kiosk_lofi_ramp_minutes: 15
kiosk_lofi_alsa_device: "default"
```

Not in the browser: `cog` has exactly one window and the dashboard is in it,
and WPE WebKit is a poor YouTube client — the player detects it and degrades or
refuses. `mpv` execs `yt-dlp` to resolve the stream and plays the audio, which
is the part actually wanted.

This box has two sound cards: the motherboard's Realtek codec and the GPU's
HDMI outputs. `aplay -l` lists them; set `kiosk_lofi_alsa_device` to the one
your speakers are on.

### yt-dlp does not come from apt

Debian trixie ships `yt-dlp` 2025.04.30. Against the configured stream that
version fails outright:

```
$ yt-dlp -f bestaudio https://www.youtube.com/watch?v=tRsQsTMvPNg
ERROR: [youtube] tRsQsTMvPNg: The page needs to be reloaded.
```

while 2026.08.19 resolves the same URL to an audio-only format without
complaint. YouTube moves faster than a stable release can follow, so the role
installs a pinned upstream version into its own venv at `/opt/kiosk/ytdlp` and
points mpv at it explicitly:

```
--script-opts=ytdl_hook-ytdl_path=/opt/kiosk/ytdlp/bin/yt-dlp
```

A venv rather than `pip --break-system-packages`, because on a Proxmox host
the system site-packages are what Proxmox's own tooling runs on. Nothing is
put on `PATH`, so nothing shadows a system binary — and mpv is told the path
rather than left to find one, since leaving it to `PATH` order is exactly how
you end up silently back on Debian's copy.

`kiosk_ytdlp_version` carries a `# renovate:` comment, so Renovate opens a PR
when upstream moves. If the stream breaks before one lands, bump that var and
re-converge:

```bash
$EDITOR ansible/group_vars/pve_host/vars.yml    # kiosk_ytdlp_version
cd ansible && ansible-playbook site.yml --limit pve_host
```

The role bounces the stream with `systemctl try-restart` afterwards, so a bump
takes effect immediately if it is currently playing and starts nothing if it
is not.

To check which end is broken:

```bash
ssh ci-deploy@pve.ts.conway-hash.com \
  '/opt/kiosk/ytdlp/bin/yt-dlp -f bestaudio --get-format "<url>"'
```

A current yt-dlp also warns that no JavaScript runtime is installed and that
some formats may therefore be missing. Audio-only extraction still works, so
`deno` is deliberately not installed — it is a large dependency for a warning
that does not currently cost anything.

---

## Before you turn it on

Nothing. This service has no credentials and no prerequisites beyond the
hypervisor itself being converged ([SETUP.md](../SETUP.md) steps 1–5).

It does assume a monitor is actually plugged in. With none attached, the daemon
still runs and still serves — you just have `cage` starting on a tty nobody is
looking at. Harmless, but pointless.

## Turning it on

```bash
$EDITOR ansible/group_vars/all/services.yml   # kiosk: true

cd ansible
ansible-playbook site.yml --limit pve_host
```

## Checking it works

```bash
ssh ci-deploy@pve.ts.conway-hash.com 'systemctl status kiosk.service'
ssh ci-deploy@pve.ts.conway-hash.com 'curl -fsS http://127.0.0.1:8099/api/fast | head -c 400'
ssh ci-deploy@pve.ts.conway-hash.com 'curl -fsS http://127.0.0.1:8099/api/slow | jq ".guests[] | {name, memory, containers}"'
```

Or just look at the screen.

## Turning it off

Set `kiosk: false` and the role stops running — but **what a previous run
already installed stays**. Ansible has no undo, and a role that installed a
package never "created" it in a sense it can reverse.

So switching it off stops the kiosk being converged; it does not remove it.
That takes one manual pass on the host:

```bash
sudo systemctl disable --now kiosk.service kiosk-lofi.timer kiosk-lofi-stop.timer kiosk-lofi.service
sudo rm -f /etc/systemd/system/kiosk.service /etc/systemd/system/kiosk-lofi*.service \
           /etc/systemd/system/kiosk-lofi*.timer
sudo systemctl daemon-reload
sudo rm -rf /opt/kiosk
sudo apt purge -y cage cog mpv alsa-utils   # jq and fonts-dejavu-core are worth keeping
```

⚠️ Also strip the autologin stanza from `/root/.bash_profile` — the role adds
it inside an `# ANSIBLE MANAGED — pve_kiosk` marker block. Leave it and tty1
keeps trying to launch a browser that is no longer installed, falling through
to `btop` every time. Remove the marked block and that stops.

## Troubleshooting

**The screen shows btop instead of the dashboard.** That is the deliberate
fallback — cage or cog failed to start. The reason is on tty1 itself, or:

```bash
ssh ci-deploy@pve.ts.conway-hash.com 'journalctl -b -t cage -t cog'
```

**The page loads but every panel says "collecting…".** The daemon is serving
but not sampling, which means a sampler thread is throwing. It prints the
exception and keeps going rather than dying, so the reason is in the journal:

```bash
ssh ci-deploy@pve.ts.conway-hash.com 'journalctl -u kiosk.service --since -1h'
```

**The guest panel is empty but the host metrics are fine.** `pvesh` needs
`pve-cluster.service`; the unit orders itself after it, but a pmxcfs that has
wedged since boot produces exactly this. `systemctl status pve-cluster`.

**A guest shows no containers.** Either it has none, or its guest agent is not
running. `qm agent <vmid> ping` from the host answers which — and the agent
also has to be enabled in the guest's config (`agent: enabled=1`), which every
guest this repo builds already is.

**The graphs reset.** Expected after `systemctl restart kiosk.service` — the
history is in memory, deliberately, so a dashboard cannot fill a disk.

**The tailnet graph is empty.** The daemon shells out to `tailscale status
--json`; if the host has dropped off the tailnet, the graph has nothing to
draw and the rest of the page still works.
