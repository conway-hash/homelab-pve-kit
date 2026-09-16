# screen — the monitor wired to the hypervisor

A browser, fullscreen, on the physical display, showing
[watch](watch.md). That is the whole service.

Runs **on** the hypervisor, because the monitor is plugged into that box — so
there is no guest to create and this is purely a role toggle.

## What it is

`cage` (a Wayland compositor that runs exactly one fullscreen client, with no
desktop or panel) launching `cog` (WebKit, built for this job), from `tty1`
via root's `.bash_profile`, which already autologins.

Not chromium: Debian's pulls a desktop's worth of dependencies onto a
hypervisor — 174 packages with recommends, and even with them off it still
drags `cups-common`, `system-config-printer` and `upower`. `cog` adds no
daemons at all.

The launch line is `cage -- cog "https://watch.ts…/" || btop`, deliberately not
`exec cage`: if cage or cog fails to start, the shell falls through to `btop`
and the monitor shows something useful instead of a black screen.

## What used to be here

`pve_kiosk` did three jobs — sample the hypervisor, serve a web page, and drive
the screen. The first two moved to the [watch](watch.md) guest, and what was
left is one package list and one line in a login profile.

This role also **removes** what the kiosk left behind: its unit, `/opt/kiosk`,
and its `.bash_profile` stanza. Ansible has no undo, so deleting a role stops
it being converged but does not uninstall it — that removal has to be written
once, somewhere, and this is it. All of it is `state: absent` and safe on a box
that never ran the kiosk.

## ⚠️ The screen now depends on another machine

The kiosk pointed at `127.0.0.1`, so the display worked whenever the host did.
This points across the tailnet at the watch guest, which means a wedged guest
leaves the monitor showing an error page — mildly ironic for a monitor.

The `|| btop` fallback only catches cog failing to **launch**, not a bad HTTP
response, and there is no honest way to make a browser fall back on content.

Accepted knowingly. The alternative is keeping a second copy of the collection
logic on the hypervisor purely so the glass stays lit, which is exactly the
duplication moving to one service was meant to remove.

## Turning it off

Set `screen: false`. The screen stops being converged, which means nothing new
is installed — but **what a previous run installed stays**, including the
`.bash_profile` stanza, so the browser keeps launching until you remove it by
hand. That asymmetry is the same one described in [README.md](README.md) and it
is not a bug in the switch: a role that installed a package never "created" it
in a sense it can reverse.
