#!/usr/bin/env python3
"""exporterd — the handful of facts only the hypervisor itself can see.

Managed by Ansible (pve_exporter) — edits will be overwritten.

## Why this exists at all

Moving the dashboard to a guest was meant to leave the hypervisor holding no
application code, and for most of the data it worked: the Proxmox API answers
for guests, storage, tasks, backups and updates. Five things it does not answer
for, at any privilege level, because they are not Proxmox's to know:

  * per-core CPU            /proc/stat
  * CPU temperature         /sys/class/hwmon
  * GPU load, VRAM, watts   /sys/class/drm
  * the tailnet             `tailscale status --json`
  * REAL guest memory       `qm guest exec <vmid> -- cat /proc/meminfo`

That last one is not cosmetic. Without a balloon device configured, Proxmox
reports the host-side RSS of the QEMU process as a guest's memory use, and the
Linux kernel fills otherwise-idle RAM with page cache — so every guest reads
90-100% within hours of booting and stays there. Measured here: watch reported
95.6% while genuinely using 27%. A number that is always red tells you nothing
and trains you to ignore the panel it sits in.

So this is the smallest thing that can answer those five questions: one
read-only endpoint, on the box that can see them.

## What it is not

There are no POST routes and no actions. `run_action`, the upgrade and reboot
buttons the kiosk carried, are deliberately not here — the dashboard already
does reboots through the Proxmox API with a token that is scoped for it, and
this service exists to be boring.

It binds to loopback and the tailnet address only. It runs as root because
`qm guest exec` and the tailscale socket require it; everything it reads is
world-readable except those two.
"""

import fnmatch
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def env(name, default=""):
    return os.environ.get(name, default)


NODE = env("EXPORTER_NODE", "pve")
PORT = int(env("EXPORTER_PORT", "9099") or 9099)
GPU_NAME_OVERRIDE = env("EXPORTER_GPU_NAME", "")
TAILNET_HIDE = [p for p in env("EXPORTER_TAILNET_HIDE", "").split(",") if p]
NODE_KINDS = json.loads(env("EXPORTER_NODE_KINDS", "{}") or "{}")
INTERVAL = float(env("EXPORTER_INTERVAL", "2") or 2)
# Guest introspection forks a process inside the guest, so it is the most
# expensive thing here and the answer changes slowly.
GUEST_INTERVAL = float(env("EXPORTER_GUEST_INTERVAL", "20") or 20)


def sh(*args, timeout=10):
    """Run a command, return stdout, never raise."""
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def read(path, default=""):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return default


def readint(path, default=0):
    try:
        return int(read(path, ""))
    except ValueError:
        return default


# ── CPU ──────────────────────────────────────────────────────────────

def cpu_times():
    """Per-core jiffy counters. Index 0 is the aggregate 'cpu' line."""
    out = []
    for line in read("/proc/stat").splitlines():
        if not line.startswith("cpu"):
            break
        f = line.split()
        vals = [int(x) for x in f[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        out.append((sum(vals), idle))
    return out


def hwmon_by_name():
    found = {}
    base = "/sys/class/hwmon"
    try:
        for entry in os.listdir(base):
            found.setdefault(read(f"{base}/{entry}/name"), f"{base}/{entry}")
    except OSError:
        pass
    return found


def cpu_temps(hw):
    """Whatever this CPU actually exposes, labelled the way the driver does.

    On the Ryzen 3600X in this box k10temp offers exactly two: Tctl (the
    control loop's value, which is what the fan curve follows) and Tccd1 (the
    die's real sensor). There is no per-core temperature to read on this part —
    asking for twelve numbers cannot produce them, so the page shows the two
    that exist rather than inventing ten.
    """
    path = hw.get("k10temp") or hw.get("coretemp")
    if not path:
        return []
    out = []
    for idx in range(1, 12):
        raw = f"{path}/temp{idx}_input"
        if not os.path.exists(raw):
            continue
        out.append({
            "label": read(f"{path}/temp{idx}_label", f"temp{idx}"),
            "c": round(readint(raw) / 1000, 1),
        })
    return out


# ── GPU ──────────────────────────────────────────────────────────────

def gpu_identify():
    """The best name available, and honest about where it came from.

    lspci reads the shared PCI ID database, where 1002:67df is one entry
    covering the entire RX 470/480/570/580/590 family — the ID genuinely does
    not distinguish them. The subsystem ID gets as far as the board partner's
    model line. EXPORTER_GPU_NAME overrides both when you know exactly which
    card is in the slot.
    """
    info = {"name": "GPU", "driver": "", "pci": ""}
    for line in sh("lspci", "-nn", "-D").splitlines():
        if re.search(r"(VGA compatible controller|3D controller)", line):
            info["pci"] = line.split()[0]
            desc = line.split(": ", 1)[-1]
            sub = sh("lspci", "-s", info["pci"], "-v")
            match = re.search(r"Subsystem: (.+)", sub)
            info["name"] = (match.group(1) if match else desc).split(" [")[0].strip()
            drv = re.search(r"Kernel driver in use: (\S+)", sub)
            info["driver"] = drv.group(1) if drv else ""
            break
    return info


GPU_INFO = gpu_identify()


def gpu_sample(hw):
    dev = None
    try:
        cards = sorted(os.listdir("/sys/class/drm"))
    except OSError:
        return None
    for card in cards:
        cand = f"/sys/class/drm/{card}/device"
        if re.fullmatch(r"card\d+", card) and os.path.exists(f"{cand}/gpu_busy_percent"):
            dev = cand
            break
    if not dev:
        return None
    mon = hw.get("amdgpu", "")
    return {
        "name": GPU_NAME_OVERRIDE or GPU_INFO["name"],
        "driver": GPU_INFO["driver"],
        "pci": GPU_INFO["pci"],
        "busy": readint(f"{dev}/gpu_busy_percent"),
        "mem_busy": readint(f"{dev}/mem_busy_percent"),
        "vram_used": readint(f"{dev}/mem_info_vram_used"),
        "vram_total": readint(f"{dev}/mem_info_vram_total"),
        "temp": round(readint(f"{mon}/temp1_input") / 1000, 1) if mon else 0,
        "fan_rpm": readint(f"{mon}/fan1_input") if mon else 0,
        "watts": round(readint(f"{mon}/power1_input") / 1e6, 1) if mon else 0,
        "sclk": round(readint(f"{mon}/freq1_input") / 1e6),
        "mclk": round(readint(f"{mon}/freq2_input") / 1e6),
    }


# ── the host itself ──────────────────────────────────────────────────

def reboot_required():
    """The actual file, which is the only authoritative answer.

    The dashboard can infer this from installed kernel versions over the API,
    and does when this service is unreachable — but inference is not the same
    as the flag Debian itself sets.
    """
    return {
        "required": os.path.exists("/var/run/reboot-required"),
        "packages": read("/var/run/reboot-required.pkgs").splitlines(),
    }


def failed_units():
    """Every failed systemd unit, not just the Proxmox-relevant set.

    /nodes/<node>/services covers what Proxmox thinks is its own; a failed
    fstrim timer or a broken tailscaled is invisible there.
    """
    return [
        line.split()[0]
        for line in sh(
            "systemctl", "list-units", "--state=failed", "--no-legend", "--plain"
        ).splitlines()
        if line.strip()
    ]


# ── guests ───────────────────────────────────────────────────────────

def guest_meminfo(vmid):
    """MemAvailable, read from inside the guest.

    Proxmox reports the host-side RSS of the QEMU process, and on Linux that is
    not the same as used: the kernel fills otherwise-idle RAM with page cache
    and hands it back the moment anything wants it. So RSS counts reclaimable
    cache as consumption and every guest reads ~100% once it has touched its
    allocation, idle or thrashing.

    Measured on this host, the same instant: Proxmox said the watch guest was
    at 95.6%; /proc/meminfo inside it said 27%. Proxmox's own UI draws the same
    misleading figure, and that it agrees is not evidence it is right.

    /proc/meminfo is the only place MemAvailable exists, and the guest agent can
    read it without SSH, a key, or a listening port.
    """
    raw = sh("qm", "guest", "exec", str(vmid), "--timeout", "8",
             "--", "cat", "/proc/meminfo", timeout=12)
    if not raw:
        return None
    try:
        out = (json.loads(raw) or {}).get("out-data", "")
    except ValueError:
        return None
    vals = {}
    for line in out.splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            parts = rest.split()
            if parts and parts[0].isdigit():
                vals[key] = int(parts[0]) * 1024   # /proc/meminfo is in kB
    if "MemTotal" in vals and "MemAvailable" in vals:
        return {
            "total": vals["MemTotal"],
            "available": vals["MemAvailable"],
            "used": vals["MemTotal"] - vals["MemAvailable"],
        }
    return None


def guest_containers(vmid):
    """What is actually running inside a guest.

    Every service in this homelab is a compose stack, so "is the guest up" and
    "is the thing you wanted up" are different questions — a guest can be
    perfectly healthy with its stack stopped. The Proxmox API cannot see inside
    a VM at all; the agent can.

    `docker ps` with a Go template rather than `--format json`, because the
    latter is a JSON OBJECT PER LINE and not a JSON array, which is a
    distinction that bites exactly once.
    """
    raw = sh("qm", "guest", "exec", str(vmid), "--timeout", "8", "--",
             "docker", "ps", "-a", "--format",
             "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}", timeout=12)
    if not raw:
        return []
    try:
        out = (json.loads(raw) or {}).get("out-data", "")
    except ValueError:
        return []
    found = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, state, status, image = parts[0], parts[1], parts[2], parts[3]
        low = status.lower()
        found.append({
            "name": name,
            "state": state,
            "status": status,
            # The image WITHOUT its registry path: the tag is the useful half
            # and the full path pushes everything else off a phone screen.
            "image": image.split("/")[-1],
            "ok": state == "running" and "unhealthy" not in low,
            "health": ("unhealthy" if "unhealthy" in low else
                       "healthy" if "healthy" in low else ""),
        })
    return found


def running_vmids():
    out = []
    for line in sh("qm", "list").splitlines()[1:]:
        f = line.split()
        if len(f) >= 3 and f[0].isdigit() and f[2] == "running":
            out.append(int(f[0]))
    return out


# ── tailnet ──────────────────────────────────────────────────────────

def tailnet(guest_names):
    raw = sh("tailscale", "status", "--json", timeout=8)
    try:
        ts = json.loads(raw) if raw else {}
    except ValueError:
        ts = {}
    if not ts.get("Self"):
        return {"ok": False, "state": "unknown", "nodes": [], "relays": []}

    relays = set()

    def kind_of(name, os_name, me, is_guest):
        """What this node physically is, for picking an icon.

        Tailscale reports an OS and nothing else, which separates a phone from
        everything with a Linux kernel and stops there — a laptop, a Raspberry
        Pi and a hypervisor are all "linux". So the two facts this box genuinely
        knows come first (it is itself; it hosts this guest), and anything finer
        has to be told to it.
        """
        if name in NODE_KINDS:
            return NODE_KINDS[name]
        if me:
            return "server"
        if is_guest:
            return "vm"
        if (os_name or "").lower() in ("android", "ios", "iphone", "ipados"):
            return "phone"
        return "desktop"

    def node(p, me=False):
        name = p.get("HostName") or "?"
        online = True if me else bool(p.get("Online"))
        direct = bool(p.get("CurAddr"))
        relay = p.get("Relay") or ""
        if online and not direct and relay and not me:
            relays.add(relay)
        is_guest = name.lower() in guest_names
        return {
            "id": p.get("ID") or name,
            "name": name,
            "dns": (p.get("DNSName") or "").rstrip("."),
            "ip": (p.get("TailscaleIPs") or [""])[0],
            "os": p.get("OS", ""),
            "self": me,
            "online": online,
            "link": "self" if me else
                    ("direct" if direct else ("relay" if online else "offline")),
            "relay": relay,
            "addr": p.get("CurAddr", ""),
            # A guest of this hypervisor is drawn inside the hypervisor's group.
            # That is the only "is hosted by" relationship this box can actually
            # observe — Headscale has no notion of one node hosting another, so
            # matching the tailnet name against the guest list is what tells us
            # vault lives on pve.
            "parent": NODE if (not me and is_guest) else None,
            "kind": kind_of(name, p.get("OS", ""), me, is_guest),
        }

    nodes = [node(ts["Self"], me=True)]
    nodes += [
        node(p) for p in (ts.get("Peer") or {}).values()
        # CI joins the tailnet as an ephemeral node for the length of a deploy
        # and is reaped afterwards. Drawing it means a machine with a run-ID for
        # a name appears on screen for five minutes and is gone before anyone
        # can ask what it was.
        if not any(fnmatch.fnmatch(p.get("HostName") or "", pat)
                   for pat in TAILNET_HIDE)
    ]
    return {
        "ok": ts.get("BackendState") == "Running",
        "state": ts.get("BackendState", "unknown"),
        "nodes": nodes,
        "relays": sorted(relays),
    }


# ── sampling ─────────────────────────────────────────────────────────

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.fast = {}
        self.slow = {}
        self.prev_cpu = cpu_times()

    def snapshot(self):
        with self.lock:
            return {**self.slow, **self.fast, "generated": time.time(), "node": NODE}


ST = State()


def fast_tick():
    hw = hwmon_by_name()
    now = cpu_times()
    with ST.lock:
        prev = ST.prev_cpu
        ST.prev_cpu = now
    cores = []
    # Index 0 is the aggregate; 1.. are the individual cores.
    for i in range(1, min(len(now), len(prev))):
        dt = now[i][0] - prev[i][0]
        di = now[i][1] - prev[i][1]
        cores.append(round(max(0.0, min(100.0, (dt - di) / dt * 100)), 1) if dt else 0.0)
    with ST.lock:
        ST.fast = {
            "cores": cores,
            "cpu_temps": cpu_temps(hw),
            "gpu": gpu_sample(hw),
        }


def slow_tick():
    guests = {}
    containers = {}
    names = set()
    for vmid in running_vmids():
        mi = guest_meminfo(vmid)
        if mi:
            guests[str(vmid)] = mi
        ctrs = guest_containers(vmid)
        if ctrs:
            containers[str(vmid)] = ctrs
    # Guest NAMES, for deciding which tailnet peers are this box's own VMs.
    for line in sh("qm", "list").splitlines()[1:]:
        f = line.split()
        if len(f) >= 2 and f[0].isdigit():
            names.add(f[1].lower())
    with ST.lock:
        ST.slow = {
            "guest_memory": guests,
            "guest_containers": containers,
            "tailnet": tailnet(names),
            "reboot": reboot_required(),
            "failed_units": failed_units(),
        }


def loop(fn, every, name):
    while True:
        started = time.time()
        try:
            fn()
        except Exception as e:                      # noqa: BLE001
            # One bad sample must not stop the loop: a collector that dies
            # silently is worse than a wrong number, because nothing is left to
            # say it happened.
            print(f"exporterd: {name} failed: {e!r}", file=sys.stderr, flush=True)
        time.sleep(max(0.5, every - (time.time() - started)))


# ── serving ──────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "exporterd"

    def log_message(self, fmt, *args):
        # One line per poll per viewer, for no benefit.
        pass

    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/metrics"):
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(ST.snapshot()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # No do_POST. This service has no actions, by design.


def main():
    threading.Thread(target=loop, args=(fast_tick, INTERVAL, "fast"), daemon=True).start()
    threading.Thread(target=loop, args=(slow_tick, GUEST_INTERVAL, "slow"), daemon=True).start()
    bind = env("EXPORTER_BIND", "127.0.0.1")
    server = ThreadingHTTPServer((bind, PORT), Handler)
    print(f"exporterd: serving on {bind}:{PORT}, node={NODE}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
