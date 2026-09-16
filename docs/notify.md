# notify — push notifications

The homelab's notification server: [ntfy](https://ntfy.sh), on its own guest
(VM 998), reachable at `notify.ts.conway-hash.com` over the tailnet and nowhere
else. Every service that needs to tell you something publishes here; the ntfy
app on your phone subscribes.

Service `notify`, role `svc_ntfy` — named for what it does, running what it is,
the same split as `vault`/`svc_vaultwarden`. If ntfy is ever swapped for
Gotify, the service keeps its name and nothing your phone points at moves.

## Why its own guest

Notifications are infrastructure, not a feature of whichever service wanted
them first. Putting ntfy inside the `watch` stack would have been less work and
would have meant that switching `watch` off silently switches off alerting for
everything else — the worst way for a monitoring system to fail, because
nothing tells you it happened.

The hostname is deliberately independent of the guest that runs it. Your phone
subscribes to `notify.ts.…`, so moving the service later costs nothing on the
client side.

## Topics

One topic per source. `watch` is the only one today; a new service picks a name
and publishes to it, with **nothing to change on this guest** — the publisher
account is granted write on `*`.

That is the point of one shared ntfy: one subscription on the phone, and each
topic mutable independently in the app, so a chatty service never forces you to
mute the one that matters.

## Accounts

Two, with opposite halves of one permission:

| Account | Permission | Who uses it |
|---|---|---|
| `publisher` | write on `*`, no read | every service that sends an alert |
| `phone` | read on `*`, no write | the ntfy app |

Split on purpose. The credential living on a device you carry cannot forge an
alert, and a service that gets compromised cannot read back what everything
else has been sending.

⚠️ `NTFY_AUTH_DEFAULT_ACCESS=deny-all` is the single most important setting on
this guest. ntfy's **default** is read-write for anonymous users — every topic
world-readable and world-writable to anything that can reach the server. On a
tailnet that still means every device you own plus anything that ever joins.
`tests/notify.sh` asserts that anonymous publish AND anonymous read are both
refused, because a server that is perfectly healthy and completely open looks
identical to a working one from the outside.

## Before you turn it on

Three credentials, in the `notify` GitHub Environment as one `SERVICE_SECRETS`
bundle (see [vault.md](vault.md) for the format):

```yaml
svc_ntfy_publisher_password: "..."   # every service publishes as this
svc_ntfy_phone_password: "..."       # the ntfy app logs in as this
svc_ntfy_cloudflare_api_token: "..." # its OWN token, not a copy of another's
tailscale_authkey: "..."             # first run only
```

`openssl rand -base64 24` is fine for both passwords.

⚠️ `svc_ntfy_publisher_password` is also needed by every service that
publishes, as `svc_watch_ntfy_password` in the `watch` bundle. One value, two
Environments — neither guest can read the other's secrets, and this is the one
place this repo knowingly tolerates a value living twice. Rotating it means
changing both.

## Setting up the phone

1. Install [ntfy](https://ntfy.sh/docs/subscribe/phone/) from F-Droid or Play
2. Settings → **Manage users** → add `notify.ts.conway-hash.com` with the
   `phone` account
3. Subscribe to the `watch` topic
4. Leave **instant delivery** on

Instant delivery holds a connection open rather than going through Firebase,
which is what makes a self-hosted server work at all. It needs Tailscale up on
the phone; if Android's battery optimiser kills either app, notifications queue
until it reconnects rather than being lost — they stay readable for
`ntfy_message_cache_duration` (72h).

## The blind spot

This guest is what tells you other things are broken, so **nothing tells you
when it is broken**. `watch` checks ntfy's HTTPS endpoint and will show it on
the dashboard, but it cannot push that particular alert anywhere — the thing it
would push through is the thing that is down.

Nor can anything here tell you the Proxmox host died, since both guests die
with it. That is not solvable from inside the homelab. It needs an external
dead-man's switch — something off-site expecting a heartbeat and going loud
when it stops. Not built; worth knowing it is a separate job.

## Turning it off

Set `notify: false`. The `pve_guests` role **destroys VM 998** on the next run
of the hypervisor play, disk included. Nothing else breaks — `watch` keeps
collecting and its dashboard keeps serving — but every alert is dropped
silently. Check `tests/watch.sh` still passes and remember you are now relying
on looking at the page.
