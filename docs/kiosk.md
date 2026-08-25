# Kiosk dashboard

Drives the monitor physically attached to the Proxmox box: host metrics,
guests, pending updates, and a live tailnet graph.

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
kiosk-data.timer  → collect.sh every 10s → /opt/kiosk/data.json
kiosk-web.service → python3 http.server, bound to 127.0.0.1 only
tty1 (autologin)  → cage -- cog http://127.0.0.1:8099/   (|| btop)
```

Nothing is served off-box: the web server binds `127.0.0.1` only, so there is
no port to firewall and nothing to authenticate.

`cog`, not chromium: Debian's chromium pulls 174 packages onto a hypervisor,
and even with recommends disabled still installs `cups-common`,
`system-config-printer` and `upower`. `cog` is WebKit built for kiosks and adds
no daemons.

The tailnet graph draws this node's own link to each peer — solid for direct,
dashed through a relay bubble for DERP, grey for offline. It's a hub, not a
mesh, because a node can only observe its own connections.

If cage or cog fails to start, tty1 falls through to `btop`, so the screen
never goes blank.

---

## Before you turn it on

Nothing. This service has no credentials and no prerequisites beyond the
hypervisor itself being converged ([SETUP.md](../SETUP.md) steps 1–5).

It does assume a monitor is actually plugged in. With none attached, the units
still run and the web server still serves — you just have `cage` starting on a
tty nobody is looking at. Harmless, but pointless.

## Turning it on

```bash
$EDITOR ansible/group_vars/all/services.yml   # kiosk: true

cd ansible
ansible-playbook site.yml --limit pve_host
```

## Checking it works

```bash
ssh ci-deploy@pve.ts.conway-hash.com 'curl -fsS http://127.0.0.1:8099/ >/dev/null && echo OK'
ssh ci-deploy@pve.ts.conway-hash.com 'systemctl status kiosk-data.timer kiosk-web.service'
ssh ci-deploy@pve.ts.conway-hash.com 'sudo cat /opt/kiosk/data.json | jq .'
```

Or just look at the screen.

## Turning it off

Set `kiosk: false` and the role stops running — but **what a previous run
already installed stays**. Ansible has no undo, and a role that installed a
package never "created" it in a sense it can reverse.

So switching it off stops the kiosk being converged; it does not remove it.
That takes one manual pass on the host:

```bash
sudo systemctl disable --now kiosk-data.timer kiosk-data.service kiosk-web.service
sudo rm -f /etc/systemd/system/kiosk-{data,web}.service /etc/systemd/system/kiosk-data.timer
sudo systemctl daemon-reload
sudo rm -rf /opt/kiosk
sudo apt purge -y cage cog                 # jq and fonts-dejavu-core are worth keeping
```

⚠️ Also strip the autologin stanza from `/root/.bash_profile` — the role adds
it inside an `# ANSIBLE MANAGED — pve_kiosk` marker block. Leave it and tty1
keeps trying to launch a browser that is no longer installed, falling through
to `btop` every time. Remove the marked block and that stops.

Then disable the tty1 autologin override if you added one by hand.

## Troubleshooting

**The screen shows btop instead of the dashboard.** That is the deliberate
fallback — cage or cog failed to start. The reason is on tty1 itself, or:

```bash
ssh ci-deploy@pve.ts.conway-hash.com 'journalctl -b -t cage -t cog'
```

**The dashboard loads but the numbers are stale.** The collector, not the
page:

```bash
ssh ci-deploy@pve.ts.conway-hash.com 'systemctl status kiosk-data.timer; journalctl -u kiosk-data.service --since -1h'
```

**The tailnet graph is empty.** `collect.sh` shells out to `tailscale status
--json`; if the host has dropped off the tailnet, the graph has nothing to
draw and the rest of the page still works.
