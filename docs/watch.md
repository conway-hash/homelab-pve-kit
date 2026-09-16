# watch — the analyser, dashboard and alert source

Reads the hypervisor over the Proxmox API, decides what needs doing, pushes it
to [ntfy](ntfy.md), and serves the dashboard at
`watch.ts.conway-hash.com`. Its own guest, VM 997.

This replaced `pve_kiosk`.

## What changed, and why it is better

The kiosk ran **on** the hypervisor and shelled out to `pvesh` and `qm`. `pvesh`
is a Perl CLI that stands the entire Proxmox API stack up on every invocation —
measured on this box at roughly **0.7s of CPU per call**, which is why the kiosk
could only afford to look at guests every 20s, and why sampling at 5s once
burned half a core continuously.

Reading the same API over **HTTP** hits `pvedaemon`, which is already running.
That costs milliseconds. Three things follow:

- **The sampling rate became a product decision** rather than a budget one. The
  dashboard offers a 1/5/10/30/60s picker.
- **Idle cost went down**, not up. The kiosk sampled every second forever
  whether or not anyone was in the room. `watch` samples every 60s when nobody
  is looking and only speeds up while a browser is actually polling.
- **The hypervisor holds no application code again.** It runs a browser
  ([screen.md](screen.md)) and nothing else.

What was lost with the screen: per-core CPU graphs, GPU temperature, and
`/proc`-level detail. All of it existed to fill a 1-second graph on a monitor,
and none of it is in an alert.

## What it watches

| Alert | Fires when |
|---|---|
| `guest_down` | a guest is not running (silence one with `watch_ignore_guests`) |
| `backup_stale` | last backup older than 8 days (warn) / 15 days (critical) |
| `backup_missing` | a running guest has no successful backup on record |
| `backup_failed` | the most recent `vzdump` for a guest did not end `OK`, and nothing has succeeded since |
| `storage_full` | any storage over 80% (warn) / 90% (critical) |
| `service_failed` | a Proxmox service is not running |
| `kernel_update` | a kernel is pending — needs a reboot, so it is a decision |
| `security_updates` / `updates` | packages pending |
| `cert_expiring` | a certificate is inside 21 days (warn) / 7 days (critical) |
| `pve_unreachable` | the API stopped answering |

The backup thresholds **must** track the schedule in
`group_vars/pve_host/vars.yml`. They were 36h/72h when backups ran nightly; on
the current weekly job every guest would have sat permanently red, which is an
alarm that means nothing.

### Where "last backed up" comes from

Two sources, preferred in this order, and the dashboard says which one answered.

**Archives** are ground truth: an archive that exists is one you can restore
from. But listing backup volumes turns out to require `Datastore.Allocate` —
measured, not assumed: with `Datastore.Audit` the API returns an empty array
and **no error**, and only `Datastore.Allocate` populates it. That privilege
also permits *deleting* volumes and removing storage configuration. Handing a
monitoring service the ability to delete the backups it watches is exactly
backwards, so the token does not have it and this source is normally empty.

**The vzdump task log** is what actually answers, and needs only `Sys.Audit`.
It records when a backup last completed and whether it succeeded.

**Only guests with an enabled backup job are judged at all.** `/cluster/backup`
says which those are, and a guest that is not on it gets no backup alerts and
shows "not scheduled" — silence is the correct output, not a gap. `watch`
itself is the example: it holds nothing that is not derived, so it has no job.

That check also contains the next problem rather than merely documenting it.

⚠️ **The task log is keyed by vmid, and vmids get recycled.** A guest created on
a vmid a destroyed guest used to hold inherits its predecessor's backup
history, which reads as "recently backed up" for a machine that has never been
backed up at all. Both new guests did exactly this on their first run, reporting
backups taken by the `links` and `finance` guests that used to hold 998 and 997.

For a guest **with** a job this self-corrects after one weekly cycle, when a
real backup produces a newer task. For a guest **without** one it never would —
nothing new would ever arrive to displace the inherited entry — which is why
unscheduled guests are excluded outright rather than trusted and watched.

If you ever decide the archive listing is worth `Datastore.Allocate`, nothing
in `watchd` needs changing: it already prefers archives whenever they are
visible, and the recycled-vmid problem disappears with them.

### The certificate alert is not a calendar reminder

Caddy renews unattended at ~30 days out, so 21 days means **renewal should have
happened and did not**. That is a real failure mode here, not a hypothetical:
see the DNS-01 propagation note in `group_vars/all/vars.yml`, where a challenge
check timed out against resolvers that could not see the record. A quietly
failing renewal is the most likely way any of these services goes down.

### Alerts fire on change, not on state

A condition notifies when it appears, when it gets worse, and when it clears.
**Staying broken is not an event.** A daemon that pushed everything wrong on
every tick would be an alarm firing once a minute forever, and the only way to
live with that is to mute it — at which point it has made things worse than
silence.

Tapping a notification opens the dashboard (ntfy's `Click` header). An alert you
cannot act on from the lock screen is one you learn to swipe away.

## What it cannot see

The Proxmox API exposes the Proxmox-relevant service set, **not every systemd
unit** on the host, and there is no API for `/var/run/reboot-required`. Reaching
those would need an SSH path from this guest back into the hypervisor.

That is deliberately not built. See **Controls** below for why.

## Controls

The dashboard can reboot the node and start/stop/reboot a guest, through the
Proxmox API. Gated by `svc_watch_control_token` as a bearer token — **tailnet
membership is not authorisation**; every device you own is on that tailnet, and
so is anything that ever joins it. Leave the token empty to disable controls
entirely: `watchd` answers 503 and the page hides the buttons.

**Applying host updates is not here**, and that is a decision rather than an
omission. There is no Proxmox API for it — Proxmox's own UI shells out — so it
would need SSH from this guest to the host. And there is no "safely limited"
version: `apt-get` runs maintainer scripts as root, so anything that can apply
updates **is root on the host**. A sudo whitelist would look like a boundary
without being one, which is the same reasoning `inventory/hosts.ini` already
uses to reject whitelists.

If that changes, the honest shape is an SSH key with a forced command (a real
boundary, unlike a sudo whitelist), every invocation pushed to ntfy so an
upgrade you did not start is visible immediately.

## Before you turn it on

### The Proxmox token

⚠️ **Not** the `homelab` token `pve_guests` uses. That one is `privsep 0` — full
control of every VM on the box — because it creates and destroys them. A
dashboard needs to read everything and power-cycle, and nothing else.

```bash
pveum role add Watch -privs "VM.Audit Datastore.Audit Sys.Audit Sys.Modify Sys.PowerMgmt"
pveum user add watch@pve
pveum acl modify / --user watch@pve --role Watch
pveum user token add watch@pve watchd --privsep 0
```

| Privilege | What it is for |
|---|---|
| `VM.Audit` | read guest status and config |
| `Datastore.Audit` | read storage usage |
| `Sys.Audit` | node status, services, task log |
| `Sys.Modify` | read pending apt updates |
| `Sys.PowerMgmt` | reboot the node — drop it if you leave controls off |

`--privsep 0` on the *token* means it inherits the user's privileges, which are
already narrow. The token secret is shown exactly once.

### The secrets bundle

In the `watch` GitHub Environment as `SERVICE_SECRETS`:

```yaml
svc_watch_pve_token_secret: "..."        # from the pveum command above
svc_watch_ntfy_password: "..."           # SAME as svc_ntfy_publisher_password
svc_watch_control_token: ""              # openssl rand -hex 32, or empty to disable
svc_watch_cloudflare_api_token: "..."    # its OWN token
tailscale_authkey: "..."                 # first run only
```

## No backup, on purpose

This guest has no `backup:` key and therefore no `vzdump` job. It holds nothing
that is not derived: history is in memory, config is in this repo, secrets are
in the Environment. Restoring it from an archive would be strictly worse than
re-running the playbook, which rebuilds it from source in minutes.

## Turning it off

Set `watch: false` — `pve_guests` **destroys VM 997** on the next run. Turn
`screen` off with it, or the monitor shows an error page pointing at a guest
that no longer exists.
