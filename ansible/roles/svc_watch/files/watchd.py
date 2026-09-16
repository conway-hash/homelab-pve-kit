#!/usr/bin/env python3
"""watchd — the homelab's analyser, dashboard and alert source.

Managed by Ansible (svc_watch) — edits will be overwritten.

This replaces the pve_kiosk daemon, which ran ON the hypervisor and shelled out
to `pvesh` and `qm`. Two things changed and both are the point:

  * It runs on a guest now and reaches the hypervisor over the Proxmox HTTP
    API. `pvesh` is a Perl CLI that stands the entire API stack up on every
    invocation — measured at ~0.7s of CPU each, which is why the kiosk could
    only afford to look at guests every 20s. The same data over HTTP hits a
    daemon that is already running, so the expensive tier stopped being
    expensive and the sampling interval became a product decision rather than
    a budget one.

  * It pushes. The kiosk drew a screen nobody was watching; the useful half of
    it was always "something needs doing", and that half now leaves the box.

Deliberately stdlib-only. The whole job is HTTP requests, JSON, a TLS
handshake and a small web server, all of which Python ships. Adding `requests`
would mean a pip layer in the image, a lockfile to maintain and a supply chain
to audit, in exchange for nothing this file needs.
"""

import base64
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


# ── Configuration ────────────────────────────────────────────────────
#
# Everything arrives through the environment, written by Ansible from
# group_vars and secrets.yml. Nothing is templated into this file, which is why
# it is a plain `files/` asset rather than a .j2 — no Jinja means no escaping
# rules to trip over in the JavaScript below, and this file is diffable as the
# Python it actually is.

def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"watchd: {name} is unset, and there is no sane default for it")
    return value


def env_int(name, default):
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        sys.exit(f"watchd: {name}={raw!r} is not an integer")


def env_list(name):
    return [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]


PVE_HOST = env("WATCH_PVE_HOST", required=True)
PVE_NODE = env("WATCH_PVE_NODE", required=True)
PVE_TOKEN_ID = env("WATCH_PVE_TOKEN_ID", required=True)
PVE_TOKEN_SECRET = env("WATCH_PVE_TOKEN_SECRET", required=True)

NTFY_URL = env("WATCH_NTFY_URL", required=True).rstrip("/")
NTFY_TOPIC = env("WATCH_NTFY_TOPIC", "watch")
NTFY_USER = env("WATCH_NTFY_USER", required=True)
NTFY_PASSWORD = env("WATCH_NTFY_PASSWORD", required=True)

# The URL a notification deep-links to. This is the whole point of the Click
# header: a buzz you cannot act on from the lock screen is a buzz you learn to
# swipe away.
DASHBOARD_URL = env("WATCH_DASHBOARD_URL", required=True).rstrip("/")

# Gates the control endpoints. Tailnet membership is NOT authorisation — every
# device you own is on that tailnet, and so is anything that ever joins it.
CONTROL_TOKEN = env("WATCH_CONTROL_TOKEN", "")

LISTEN_PORT = env_int("WATCH_PORT", 8080)

# ── Sampling ──
#
# Two speeds, picked by whether anyone is actually looking. The kiosk sampled
# at one second forever, whether or not a human was in the room, and burned
# half a core doing it. Here the fast rate applies only while a browser is
# polling, and the box idles the rest of the time — which is almost always.
IDLE_SECONDS = env_int("WATCH_IDLE_SECONDS", 60)
ACTIVE_GRACE = env_int("WATCH_ACTIVE_GRACE_SECONDS", 90)
MIN_SECONDS = env_int("WATCH_MIN_SECONDS", 1)
HISTORY_POINTS = env_int("WATCH_HISTORY_POINTS", 900)

# ── Thresholds ──
#
# These MUST track the backup schedule in group_vars/pve_host/vars.yml. They
# were 36h/72h while backups ran nightly; on a weekly job every guest would
# have sat permanently red, which is an alarm that means nothing and trains you
# to ignore the one that matters.
BACKUP_WARN_HOURS = env_int("WATCH_BACKUP_WARN_HOURS", 192)
BACKUP_BAD_HOURS = env_int("WATCH_BACKUP_BAD_HOURS", 360)
DISK_WARN_PCT = env_int("WATCH_DISK_WARN_PCT", 80)
DISK_BAD_PCT = env_int("WATCH_DISK_BAD_PCT", 90)

# Caddy renews at ~30 days out, unattended. This is not a calendar reminder —
# it is "renewal should have happened by now and did not", which is the failure
# mode that actually takes a service down. See docs/watch.md.
CERT_WARN_DAYS = env_int("WATCH_CERT_WARN_DAYS", 21)
CERT_BAD_DAYS = env_int("WATCH_CERT_BAD_DAYS", 7)

TLS_HOSTS = env_list("WATCH_TLS_HOSTS")

# Guests that are expected to be off. Without this, a guest you deliberately
# stopped alerts forever and the only way to silence it is to start it.
IGNORE_GUESTS = set(env_list("WATCH_IGNORE_GUESTS"))


# ── Proxmox API ──────────────────────────────────────────────────────

class ProxmoxError(Exception):
    pass


class Proxmox:
    """The read half of the Proxmox API, plus the few writes the controls need.

    Authentication is an API token, not a ticket: tokens do not expire, carry
    no CSRF requirement for the endpoints used here, and can be scoped to a
    role. The one this runs as can read guests, storage, tasks and pending
    updates, and power-cycle — and deliberately nothing else. In particular it
    does NOT hold Datastore.Allocate, which is what listing backup ARCHIVES
    turns out to require and which also permits deleting them. See the backup
    section of collect().
    """

    def __init__(self):
        self.base = f"https://{PVE_HOST}:8006/api2/json"
        # Proxmox ships a self-signed certificate and this connection only ever
        # crosses the tailnet, which is already authenticated and encrypted at
        # the WireGuard layer. Verifying a certificate nobody issued would buy
        # nothing — the same reasoning as pve_api_validate_certs in group_vars.
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def _request(self, path, data=None, method=None):
        url = self.base + path
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header(
            "Authorization",
            f"PVEAPIToken={PVE_TOKEN_ID}={PVE_TOKEN_SECRET}",
        )
        try:
            with urllib.request.urlopen(req, timeout=20, context=self.ctx) as r:
                return json.loads(r.read().decode()).get("data")
        except urllib.error.HTTPError as e:
            # 401/403 here almost always means the token lost a privilege
            # rather than that the endpoint is broken, so say which.
            raise ProxmoxError(f"{method or 'GET'} {path} -> HTTP {e.code}") from e
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            raise ProxmoxError(f"{method or 'GET'} {path} -> {e}") from e
        except json.JSONDecodeError as e:
            raise ProxmoxError(f"{method or 'GET'} {path} -> malformed JSON") from e

    def get(self, path):
        return self._request(path)

    def post(self, path, **data):
        return self._request(path, data=data, method="POST")

    # ── the individual reads ──

    def node_status(self):
        return self.get(f"/nodes/{PVE_NODE}/status") or {}

    def guests(self):
        return self.get(f"/nodes/{PVE_NODE}/qemu") or []

    def storages(self):
        return self.get(f"/nodes/{PVE_NODE}/storage") or []

    def backups(self, storage):
        return self.get(
            f"/nodes/{PVE_NODE}/storage/{storage}/content?content=backup"
        ) or []

    def tasks(self, limit=100):
        # typefilter, so the window is 100 BACKUP tasks rather than 100 tasks of
        # any kind — on a busy node the vzdump entries would otherwise be pushed
        # out by routine work within days.
        return self.get(
            f"/nodes/{PVE_NODE}/tasks?typefilter=vzdump&limit={limit}&source=all"
        ) or []

    def pending_updates(self):
        return self.get(f"/nodes/{PVE_NODE}/apt/update") or []

    def services(self):
        return self.get(f"/nodes/{PVE_NODE}/services") or []


PVE = Proxmox()


# ── TLS expiry ───────────────────────────────────────────────────────

def cert_days_left(host, port=443, timeout=8):
    """Days until `host`'s certificate expires, or None if it cannot be read.

    A plain handshake — no request is sent, so nothing is logged as traffic on
    the far end. Verification is left ON: a certificate that does not verify is
    one of the things worth knowing about, and skipping the check would hide it.
    """
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                not_after = tls.getpeercert()["notAfter"]
    except (ssl.SSLError, ssl.CertificateError, OSError, KeyError, TypeError):
        return None
    try:
        expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return (expires - datetime.now(timezone.utc)).total_seconds() / 86400


# ── Collection ───────────────────────────────────────────────────────

def _pct(used, total):
    return round(used / total * 100, 1) if total else 0.0


def collect():
    """One full read of the world. Returns the state the page and the alerts
    both work from — computed once, so the screen and the notification can
    never disagree about what is true."""
    now = time.time()
    state = {"generated": now, "node": PVE_NODE, "errors": []}

    def attempt(key, fn, default):
        try:
            return fn()
        except ProxmoxError as e:
            # One unreachable endpoint must not blank the whole dashboard. The
            # error is surfaced as data, which is also what raises the
            # `pve_unreachable` alert below.
            state["errors"].append(f"{key}: {e}")
            return default

    status = attempt("status", PVE.node_status, {})
    mem = status.get("memory") or {}
    root = status.get("rootfs") or {}
    state["host"] = {
        "cpu": round((status.get("cpu") or 0) * 100, 1),
        "cores": (status.get("cpuinfo") or {}).get("cpus"),
        "loadavg": status.get("loadavg") or [],
        "uptime": status.get("uptime"),
        "kernel": status.get("current-kernel") or {},
        "pveversion": status.get("pveversion"),
        "memory": {
            "used": mem.get("used", 0),
            "total": mem.get("total", 0),
            "pct": _pct(mem.get("used", 0), mem.get("total", 0)),
        },
        "rootfs": {
            "used": root.get("used", 0),
            "total": root.get("total", 0),
            "pct": _pct(root.get("used", 0), root.get("total", 0)),
        },
    }

    # ── guests ──
    guests = []
    for g in attempt("guests", PVE.guests, []):
        if g.get("template"):
            continue
        guests.append({
            "vmid": g.get("vmid"),
            "name": g.get("name") or str(g.get("vmid")),
            "status": g.get("status"),
            "running": g.get("status") == "running",
            "uptime": g.get("uptime") or 0,
            "cpu": round((g.get("cpu") or 0) * 100, 1),
            "memory": {
                "used": g.get("mem", 0),
                "total": g.get("maxmem", 0),
                "pct": _pct(g.get("mem", 0), g.get("maxmem", 0)),
            },
        })
    guests.sort(key=lambda g: g["vmid"] or 0)
    state["guests"] = guests

    # ── storage ──
    stores = []
    backup_stores = []
    for s in attempt("storages", PVE.storages, []):
        if not s.get("active"):
            continue
        used, total = s.get("used", 0), s.get("total", 0)
        stores.append({
            "name": s.get("storage"),
            "type": s.get("type"),
            "used": used,
            "total": total,
            "pct": _pct(used, total),
        })
        if "backup" in (s.get("content") or ""):
            backup_stores.append(s.get("storage"))
    stores.sort(key=lambda s: s["name"] or "")
    state["storage"] = stores

    # ── backups ──
    #
    # Two sources, because neither is sufficient alone.
    #
    # ARCHIVES are ground truth: an archive that exists is one you can restore
    # from. But listing backup volumes requires Datastore.Allocate, which also
    # permits DELETING volumes and removing storage configuration. Measured, not
    # assumed: with Datastore.Audit the listing returns an empty array and no
    # error, and only Datastore.Allocate populates it. Handing a monitoring
    # service the ability to delete the backups it watches is exactly backwards,
    # so the token does not have it and this list is normally empty.
    #
    # TASKS are what we actually get: the vzdump task log needs only Sys.Audit
    # and says when a backup last completed, and whether it succeeded.
    #
    # ⚠️ The task log is keyed by vmid, and vmids get RECYCLED. A guest created
    # on a vmid that a destroyed guest used to hold inherits its predecessor's
    # backup history — which reads as "recently backed up" for a machine that
    # has never been backed up at all. That is a false negative on the alert
    # that matters most, and it resolves itself only once the new guest's first
    # real backup runs. Archives do not have this problem, which is why they are
    # preferred whenever they are visible.
    newest = {}
    for store in backup_stores:
        for vol in attempt(f"backups/{store}", lambda s=store: PVE.backups(s), []):
            vmid = vol.get("vmid")
            if vmid is None:
                continue
            vmid = int(vmid)
            ctime = vol.get("ctime") or 0
            if ctime > newest.get(vmid, {}).get("ctime", 0):
                newest[vmid] = {
                    "ctime": ctime,
                    "size": vol.get("size", 0),
                    "storage": store,
                }
    state["backups"] = {str(k): v for k, v in newest.items()}

    # ── vzdump outcomes ──
    #
    # Newest SUCCESS and newest ATTEMPT are tracked separately. A stale archive
    # and a failed job are different problems: the first says nothing ran, the
    # second says something ran and broke. Collapsing them would let a job that
    # fails every week look merely old.
    success, attempt_ = {}, {}
    for t in attempt("tasks", PVE.tasks, []):
        if not t.get("endtime"):
            continue
        vmid = (t.get("id") or "").split("@")[0]
        if not vmid.isdigit():
            continue
        vmid = int(vmid)
        ok = (t.get("status") or "").upper() == "OK"
        rec = {"endtime": t["endtime"], "status": t.get("status") or "", "ok": ok}
        if t["endtime"] > attempt_.get(vmid, {}).get("endtime", 0):
            attempt_[vmid] = rec
        if ok and t["endtime"] > success.get(vmid, {}).get("endtime", 0):
            success[vmid] = rec
    state["vzdump"] = {str(k): v for k, v in attempt_.items()}

    # ── when each guest was last backed up ──
    #
    # One answer per guest, with the source named, so the dashboard and the
    # alert cannot disagree about which evidence they are quoting.
    last = {}
    for g in guests:
        vmid = g["vmid"]
        arc = newest.get(vmid)
        suc = success.get(vmid)
        if arc:
            last[str(vmid)] = {"t": arc["ctime"], "source": "archive",
                               "storage": arc["storage"], "size": arc["size"]}
        elif suc:
            last[str(vmid)] = {"t": suc["endtime"], "source": "task"}
    state["backup_last"] = last

    # ── pending updates ──
    updates = attempt("updates", PVE.pending_updates, [])
    kernel_re = re.compile(r"^(proxmox-kernel|pve-kernel|linux-image)")
    state["updates"] = {
        "count": len(updates),
        "packages": sorted(u.get("Package", "") for u in updates)[:50],
        # A pending kernel is the one update that also implies a reboot, which
        # is a different decision from "apply this" and worth flagging apart.
        "kernel": sorted(
            u.get("Package", "") for u in updates
            if kernel_re.match(u.get("Package", "") or "")
        ),
        "security": sorted(
            u.get("Package", "") for u in updates
            if "security" in (u.get("Origin", "") or "").lower()
        ),
    }

    # ── Proxmox's own services ──
    #
    # NOT every systemd unit on the box: the API exposes the Proxmox-relevant
    # set, and reaching the rest would mean an SSH path from this guest back
    # into the hypervisor. That trade is documented in docs/watch.md.
    failed = []
    for s in attempt("services", PVE.services, []):
        if s.get("state") not in ("running", "dead", None):
            failed.append({"name": s.get("name"), "state": s.get("state")})
        elif s.get("active-state") == "failed":
            failed.append({"name": s.get("name"), "state": "failed"})
    state["failed_services"] = failed

    # ── certificates ──
    certs = []
    for host in TLS_HOSTS:
        days = cert_days_left(host)
        certs.append({
            "host": host,
            "days": round(days, 1) if days is not None else None,
        })
    state["certs"] = certs

    return state


# ── Alert evaluation ─────────────────────────────────────────────────
#
# Each check yields (key, level, title, body). The KEY is the identity of the
# condition — one per guest, per storage, per host — and is what the notifier
# tracks so that a problem which stays broken does not re-notify every minute.

LEVELS = {"ok": 0, "info": 1, "warn": 2, "critical": 3}
NTFY_PRIORITY = {"info": "2", "warn": "4", "critical": "5"}
NTFY_TAGS = {"info": "information_source", "warn": "warning", "critical": "rotating_light"}


def _age_hours(ts):
    return (time.time() - ts) / 3600 if ts else None


def evaluate(state):
    out = []

    if state["errors"]:
        out.append((
            "pve_unreachable", "critical",
            "Cannot read the hypervisor",
            "The Proxmox API did not answer:\n" + "\n".join(state["errors"][:5]),
        ))

    for g in state["guests"]:
        vmid, name = g["vmid"], g["name"]
        if not g["running"] and name not in IGNORE_GUESTS and str(vmid) not in IGNORE_GUESTS:
            out.append((
                f"guest_down:{vmid}", "critical",
                f"{name} is {g['status']}",
                f"VM {vmid} ({name}) is not running.",
            ))

        b = state["backup_last"].get(str(vmid))
        age = _age_hours(b["t"]) if b else None
        # Where the evidence came from, said out loud — an archive is proof you
        # can restore, a task is only proof something once reported success.
        via = ""
        if b:
            via = (f"Newest archive on {b['storage']}" if b["source"] == "archive"
                   else "Last successful vzdump task")
        if b is None:
            # Only worth saying for a guest that is up. A machine that does not
            # exist yet having no backup is not news.
            if g["running"]:
                out.append((
                    f"backup_missing:{vmid}", "warn",
                    f"{name} has never been backed up",
                    f"No successful vzdump for VM {vmid}, and no archive "
                    f"visible on any backup storage.",
                ))
        elif age > BACKUP_BAD_HOURS:
            out.append((
                f"backup_stale:{vmid}", "critical",
                f"{name}'s backup is {age / 24:.0f} days old",
                f"{via} is {age / 24:.1f} days old. That is two missed runs "
                f"or more.",
            ))
        elif age > BACKUP_WARN_HOURS:
            out.append((
                f"backup_stale:{vmid}", "warn",
                f"{name}'s backup is {age / 24:.0f} days old",
                f"{via} is {age / 24:.1f} days old — the weekly job looks like "
                f"it missed its slot.",
            ))

        # A failure only counts if it is the most recent thing that happened.
        # An old failure followed by a success is history, not a problem — and
        # without the comparison a single bad night would alert forever.
        z = state["vzdump"].get(str(vmid))
        if z and not z["ok"] and (b is None or z["endtime"] >= b["t"]):
            out.append((
                f"backup_failed:{vmid}", "critical",
                f"{name}'s last backup FAILED",
                f"vzdump finished with: {z['status']}",
            ))

    for s in state["storage"]:
        if s["pct"] >= DISK_BAD_PCT:
            out.append((
                f"storage_full:{s['name']}", "critical",
                f"{s['name']} is {s['pct']}% full",
                f"{s['name']} ({s['type']}) has almost no room left. "
                f"If this is the hypervisor's root filesystem, Proxmox stops "
                f"working when it fills.",
            ))
        elif s["pct"] >= DISK_WARN_PCT:
            out.append((
                f"storage_full:{s['name']}", "warn",
                f"{s['name']} is {s['pct']}% full",
                f"{s['name']} ({s['type']}) is filling up.",
            ))

    for f in state["failed_services"]:
        out.append((
            f"service_failed:{f['name']}", "critical",
            f"{f['name']} is {f['state']}",
            f"A Proxmox service on {PVE_NODE} is not running.",
        ))

    u = state["updates"]
    if u["kernel"]:
        out.append((
            "kernel_update", "warn",
            "Kernel update pending",
            "A new kernel is waiting: " + ", ".join(u["kernel"]) +
            "\nApplying it needs a reboot, so this one is a decision rather "
            "than a routine patch.",
        ))
    elif u["security"]:
        out.append((
            "security_updates", "warn",
            f"{len(u['security'])} security update(s) pending",
            ", ".join(u["security"][:20]),
        ))
    elif u["count"]:
        out.append((
            "updates", "info",
            f"{u['count']} update(s) pending",
            ", ".join(u["packages"][:20]),
        ))

    for c in state["certs"]:
        if c["days"] is None:
            out.append((
                f"cert_unreadable:{c['host']}", "warn",
                f"Cannot read {c['host']}'s certificate",
                "The TLS handshake failed. Either the host is down or it is "
                "serving a certificate that does not verify.",
            ))
        elif c["days"] < CERT_BAD_DAYS:
            out.append((
                f"cert_expiring:{c['host']}", "critical",
                f"{c['host']}'s certificate expires in {c['days']:.0f} days",
                "Caddy renews unattended at ~30 days out, so this means "
                "renewal has been failing for weeks.",
            ))
        elif c["days"] < CERT_WARN_DAYS:
            out.append((
                f"cert_expiring:{c['host']}", "warn",
                f"{c['host']}'s certificate expires in {c['days']:.0f} days",
                "Caddy should have renewed this by now. Check its logs before "
                "it becomes an outage.",
            ))

    return out


# ── Notification ─────────────────────────────────────────────────────

class Notifier:
    """Pushes state CHANGES, not state.

    The difference is the whole design. A daemon that pushed everything wrong
    on every tick would be an outage alarm that fires once a minute forever,
    and the only way to live with it is to mute it — at which point it has
    made things worse than silence.

    So a condition notifies when it appears, when it gets worse, and when it
    clears. Staying broken is not an event.
    """

    def __init__(self):
        self.active = {}          # key -> level
        self.lock = threading.Lock()
        self.log = deque(maxlen=100)

    def push(self, title, body, level="info", tags=None, click=None):
        data = body.encode("utf-8")
        req = urllib.request.Request(f"{NTFY_URL}/{NTFY_TOPIC}", data=data, method="POST")
        creds = base64.b64encode(
            f"{NTFY_USER}:{NTFY_PASSWORD}".encode()
        ).decode()
        req.add_header("Authorization", f"Basic {creds}")
        # Headers, not a JSON body: ntfy's header API keeps the body as the
        # literal message text, so a message containing a brace or a quote
        # cannot change the shape of the request.
        req.add_header("Title", title)
        req.add_header("Priority", NTFY_PRIORITY.get(level, "3"))
        req.add_header("Tags", tags or NTFY_TAGS.get(level, "bell"))
        # Tapping the notification lands on the dashboard. Without this the
        # alert tells you something is wrong and then leaves you to go and find
        # the page yourself, which is the difference between useful and noise.
        req.add_header("Click", click or DASHBOARD_URL)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read()
            self.log.append({"t": time.time(), "title": title, "level": level, "ok": True})
            return True
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            # A push that cannot be delivered must not take the daemon down —
            # the dashboard is still serving and still correct.
            print(f"watchd: ntfy push failed: {e}", file=sys.stderr, flush=True)
            self.log.append({"t": time.time(), "title": title, "level": level,
                             "ok": False, "error": str(e)})
            return False

    def reconcile(self, alerts):
        seen = {}
        for key, level, title, body in alerts:
            seen[key] = (level, title, body)

        with self.lock:
            for key, (level, title, body) in seen.items():
                previous = self.active.get(key)
                if previous == level:
                    continue          # still broken, in the same way. Not news.
                if previous is None or LEVELS[level] > LEVELS[previous]:
                    self.push(title, body, level=level)
                self.active[key] = level

            for key in [k for k in self.active if k not in seen]:
                level = self.active.pop(key)
                self.push(
                    "Resolved: " + key.split(":")[0].replace("_", " "),
                    f"{key} is clear again.",
                    level="info",
                    tags="white_check_mark",
                )
        return seen


NOTIFIER = Notifier()


# ── Shared state ─────────────────────────────────────────────────────

class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.seq = 0
        self.history = deque(maxlen=HISTORY_POINTS)
        self.latest = {}
        self.alerts = []
        # Set by every client poll. The sampler reads it to decide whether
        # anyone is actually watching.
        self.last_client = 0.0
        self.client_interval = IDLE_SECONDS

    def publish(self, state, alerts):
        with self.lock:
            self.seq += 1
            state["seq"] = self.seq
            self.latest = state
            self.alerts = [
                {"key": k, "level": lv, "title": t, "body": b}
                for k, lv, t, b in alerts
            ]
            self.history.append({
                "seq": self.seq,
                "t": state["generated"],
                "cpu": state["host"]["cpu"],
                "mem": state["host"]["memory"]["pct"],
                "guests": {
                    str(g["vmid"]): {"cpu": g["cpu"], "mem": g["memory"]["pct"]}
                    for g in state["guests"]
                },
            })

    def since(self, seq):
        with self.lock:
            return {
                "latest": self.latest,
                "alerts": self.alerts,
                # Everything the caller has not seen. A client polling every 30s
                # against a 1s sampler still gets all 30 points, so a slower poll
                # costs requests rather than resolution.
                "history": [h for h in self.history if h["seq"] > seq],
                "seq": self.seq,
                "meta": {
                    "idle_seconds": IDLE_SECONDS,
                    "min_seconds": MIN_SECONDS,
                    "ntfy_url": NTFY_URL,
                    "ntfy_topic": NTFY_TOPIC,
                    "controls": bool(CONTROL_TOKEN),
                },
            }

    def note_client(self, interval):
        with self.lock:
            self.last_client = time.time()
            if interval:
                self.client_interval = max(MIN_SECONDS, min(interval, IDLE_SECONDS))

    def current_interval(self):
        with self.lock:
            watching = (time.time() - self.last_client) < ACTIVE_GRACE
            return self.client_interval if watching else IDLE_SECONDS


ST = Store()


# ── The sampler ──────────────────────────────────────────────────────

def sampler():
    while True:
        started = time.time()
        try:
            state = collect()
            alerts = evaluate(state)
            ST.publish(state, alerts)
            NOTIFIER.reconcile(alerts)
        except Exception as e:                      # noqa: BLE001
            # A bug in one check must not stop the loop: a monitor that dies
            # silently is worse than one that reports a wrong number, because
            # nothing is left to tell you it happened.
            print(f"watchd: sampler error: {e!r}", file=sys.stderr, flush=True)
        elapsed = time.time() - started
        time.sleep(max(0.5, ST.current_interval() - elapsed))


# ── HTTP ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "watchd"

    def log_message(self, fmt, *args):
        # The default handler logs every request to stderr, which on a 1s poll
        # is a line per second per viewer for no benefit — Caddy already keeps
        # an access log in front of this.
        pass

    def _send(self, code, payload, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        args = urllib.parse.parse_qs(query)

        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, {"error": "dashboard missing"})

        if path == "/healthz":
            # Deliberately does NOT depend on Proxmox being reachable. This
            # answers "is watchd serving", which is what the self-heal timer
            # must act on; whether the hypervisor is answering is an ALERT, not
            # a reason to restart this container in a loop.
            return self._send(200, {"ok": True, "seq": ST.seq})

        if path == "/api/state":
            seq = int((args.get("since") or ["0"])[0] or 0)
            interval = int((args.get("interval") or ["0"])[0] or 0)
            ST.note_client(interval)
            return self._send(200, ST.since(seq))

        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/control":
            return self._send(404, {"error": "not found"})

        # Tailnet membership is not authorisation. Every device on this tailnet
        # — phone, laptop, anything that joins later — can reach this port, and
        # the actions below power-cycle real machines.
        if not CONTROL_TOKEN:
            return self._send(503, {"error": "controls are disabled: no token configured"})
        supplied = self.headers.get("Authorization", "")
        if not supplied.startswith("Bearer ") or not _constant_eq(
            supplied[7:], CONTROL_TOKEN
        ):
            return self._send(401, {"error": "unauthorized"})

        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "malformed body"})

        action = body.get("action")
        vmid = body.get("vmid")
        try:
            result = run_control(action, vmid)
        except ProxmoxError as e:
            return self._send(502, {"error": str(e)})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        return self._send(200, {"ok": True, "result": result})


def _constant_eq(a, b):
    """Compare without leaking length or position through timing."""
    if len(a) != len(b):
        return False
    diff = 0
    for x, y in zip(a.encode(), b.encode()):
        diff |= x ^ y
    return diff == 0


def run_control(action, vmid=None):
    """The small set of things this service may change.

    Reboot and guest power only. Applying host updates is deliberately NOT here
    — there is no Proxmox API for it, and the SSH path it would need makes this
    guest root on the hypervisor. See "Controls" in docs/watch.md.
    """
    if action == "reboot-node":
        NOTIFIER.push(
            f"Rebooting {PVE_NODE}",
            "A reboot was requested from the watch dashboard.",
            level="warn", tags="warning",
        )
        return PVE.post(f"/nodes/{PVE_NODE}/status", command="reboot")

    if action in ("guest-start", "guest-stop", "guest-reboot", "guest-shutdown"):
        if not str(vmid or "").isdigit():
            raise ValueError("vmid is required and must be numeric")
        verb = action.split("-", 1)[1]
        NOTIFIER.push(
            f"{verb} requested for VM {vmid}",
            "Requested from the watch dashboard.",
            level="info", tags="gear",
        )
        return PVE.post(f"/nodes/{PVE_NODE}/qemu/{vmid}/status/{verb}")

    raise ValueError(f"unknown action {action!r}")


def main():
    threading.Thread(target=sampler, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(
        f"watchd: serving on :{LISTEN_PORT}, node={PVE_NODE}, "
        f"idle={IDLE_SECONDS}s, topic={NTFY_TOPIC}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
