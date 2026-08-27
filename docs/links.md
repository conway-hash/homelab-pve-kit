# Linkwarden

Bookmarks on your own machine, with the browser's own bookmark tree synced
both ways so the same links are there in Firefox, in Chrome, and on a phone.

```yaml
# ansible/group_vars/all/services.yml
service_enabled:
  links: true
```

| | |
|---|---|
| Flag | `links` |
| Runs on | its own guest, VM 998, created by the `pve_guests` role |
| Roles | `common`, `tailscale`, `docker`, `svc_linkwarden` |
| Settings | `ansible/group_vars/links_host/vars.yml` |
| Secrets | `ansible/group_vars/links_host/secrets.yml` (git-ignored) |
| Reachable at | `https://links.ts.conway-hash.com`, tailnet only |
| Backed up to | `tank`, nightly 04:00, zstd, 7 rolling dailies |

## Why Linkwarden and not linkding

linkding is the lighter, simpler program — one container and a SQLite file
against Linkwarden's four containers and a Postgres — and if the goal were
only "a searchable list of links on my own box", it would be the better
answer.

It is not the goal. The point of moving off Google's bookmarks is **not being
tied to one browser's sync**, which means the browser's own bookmark tree has
to be the thing that syncs, in both directions, on every machine. That job
belongs to [floccus](https://floccus.org/), and floccus supports exactly these
self-hostable backends:

> Nextcloud Bookmarks, Linkwarden, KaraKeep, Google Drive, Dropbox, any Git
> server, or any WebDAV-compatible service

**linkding is not on that list**, and its own extension does something
different — it saves the current page to the server and searches what is
already there. Good, but it leaves the bookmark bar exactly where it was.

Karakeep would also work, and adds AI tagging and full-text search of page
content. It was passed over because the tagging wants an OpenAI key or a
local Ollama, and this repo's whole selling point is needing neither.

```
Firefox bookmark tree  <--floccus-->  links.ts.conway-hash.com  <--floccus-->  Chrome bookmark tree
```

## Why its own Cloudflare token

Every service secret is named `SVC_<SERVICE>_<THING>`, and the next service
wanting a Caddy gets its **own** Cloudflare token rather than a share of the
vault's. Same zone, same permissions — but revoking one does not take the
other down, and the Cloudflare audit log can say which service wrote which
record. Two credentials is the point, not an oversight.

---

## Before you turn it on

The hypervisor must already be set up ([SETUP.md](../SETUP.md) steps 1–6,
including the Proxmox API token, because this service brings a guest).

Then five credentials. Four you generate, one you mint at Cloudflare:

```bash
# 1-3. Three unrelated random secrets. They must be different from each other.
openssl rand -base64 32                 # NEXTAUTH_SECRET  (signs session cookies)
openssl rand -base64 32 | tr -d '/+='   # POSTGRES_PASSWORD — keep it URL-safe
openssl rand -base64 32                 # MEILI_MASTER_KEY (gates the search index)

# 4. A NON-ephemeral Headscale pre-auth key for the guest itself.
sudo docker exec headscale headscale preauthkeys create --user 1
```

5. A Cloudflare API token for the ACME DNS-01 challenge. Use the **"Edit zone
   DNS" template**, restricted to the one zone — that grants Zone:Read +
   DNS:Edit, and both halves are load-bearing: the plugin walks up from
   `_acme-challenge.links.ts.conway-hash.com` to find the owning zone, which
   DNS:Edit alone cannot do. Created at
   <https://dash.cloudflare.com/profile/api-tokens>.

⚠️ The pre-auth key is **not** the same as CI's `TS_AUTHKEY`. The runner's is
ephemeral so a throwaway node reaps itself; this one is non-ephemeral because
an ephemeral key makes a permanent machine delete itself the moment it
disconnects.

`POSTGRES_PASSWORD` is kept URL-safe on purpose. It is interpolated into a
`postgresql://` connection string; the template percent-encodes it, but a
password full of punctuation is a miserable thing to debug through two layers
of encoding when something goes wrong.

### For CI

| Secret | What |
|---|---|
| `SVC_LINKWARDEN_NEXTAUTH_SECRET` | from step 1 |
| `SVC_LINKWARDEN_POSTGRES_PASSWORD` | from step 2 |
| `SVC_LINKWARDEN_MEILI_MASTER_KEY` | from step 3 |
| `SVC_LINKWARDEN_TS_AUTHKEY` | from step 4 — first run only; once joined, the role skips it |
| `SVC_LINKWARDEN_CLOUDFLARE_API_TOKEN` | from step 5 |

The three generated ones can be created and stored in a single command each,
so the secret never lands in your shell history, your scrollback, or a file:

```bash
gh secret set SVC_LINKWARDEN_NEXTAUTH_SECRET   --body "$(openssl rand -base64 32)"
gh secret set SVC_LINKWARDEN_POSTGRES_PASSWORD --body "$(openssl rand -base64 32 | tr -d '/+=')"
gh secret set SVC_LINKWARDEN_MEILI_MASTER_KEY  --body "$(openssl rand -base64 32)"
```

You never need to see these three again — nothing reads them but the
containers. The remaining two come from elsewhere, so they are pasted in:

```bash
gh secret set SVC_LINKWARDEN_TS_AUTHKEY             # the Headscale key, step 4
gh secret set SVC_LINKWARDEN_CLOUDFLARE_API_TOKEN   # the Cloudflare token, step 5
```

### For a local run

```bash
cd ansible
cp group_vars/links_host/secrets.yml.example group_vars/links_host/secrets.yml
$EDITOR group_vars/links_host/secrets.yml
```

## Turning it on

```bash
$EDITOR ansible/group_vars/all/services.yml   # links: true
git commit -am 'links: switch on' && git push
```

It ships **off** because it needs those five secrets first, and a service
switched on without them is a deploy that fails at the first `assert` rather
than a service. The asserts name the fix.

The first run is slow — it clones a 40G disk, compiles Caddy with the
Cloudflare DNS plugin, waits on an ACME issuance, then runs the app's database
migrations. The playbook's last task polls the real URL for up to ten minutes.

## Then: the registration dance

Linkwarden ships with registration **open**, which on a tailnet-reachable box
means anything that joins can create itself an account. So this repo ships it
**closed** — which also means your own first account cannot be created yet.

One-time, in this order:

```bash
# 1. Open registration, just long enough to make your account.
$EDITOR ansible/group_vars/links_host/vars.yml   # linkwarden_disable_registration: false
git commit -am 'links: open registration to create the first account' && git push

# 2. Register at https://links.ts.conway-hash.com — the FIRST account is
#    user id 1, which is what NEXT_PUBLIC_ADMIN=1 makes the administrator.

# 3. Close it again. Do not skip this.
$EDITOR ansible/group_vars/links_host/vars.yml   # linkwarden_disable_registration: true
git commit -am 'links: close registration' && git push
```

The deploy's smoke test fails the run if registration is left open, so step 3
is enforced rather than merely remembered.

## Importing your Google bookmarks

Export from Chrome first — `chrome://bookmarks` → ⋮ → **Export bookmarks**,
which writes the standard Netscape bookmark HTML. Then in Linkwarden:
**Settings → Import & Export → Import from → Bookmarks HTML file**.

Caddy's request body limit is raised to `linkwarden_max_upload` (100MB) for
exactly this: the default 10MB rejects the export from a long-lived account,
and the browser shows only a generic failure.

## Setting up floccus

Install floccus in each browser
([Firefox](https://addons.mozilla.org/en-US/firefox/addon/floccus/),
[Chrome](https://chromewebstore.google.com/detail/floccus-bookmarks-sync/fnaicdffflnofjppbagibeoednhnbjhg)),
add an account, choose **Linkwarden** as the adapter, and give it:

- **Server**: `https://links.ts.conway-hash.com`
- **API key**: Linkwarden → Settings → Access Tokens → new token

⚠️ Every browser you sync from must be **on the tailnet** — the name resolves
through MagicDNS and the address is reachable nowhere else. That is the
trade this whole repo makes: nothing is published, so nothing is reachable
without joining.

## Staying up

The same five layers as the vault, for the same reasons:

| What broke | What handles it |
|---|---|
| The host was powered off and on | `onboot` + a startup order, set by `pve_guests` |
| The guest rebooted | `restart: unless-stopped`, plus a systemd unit for the stack |
| A container's process exited | Docker's restart policy |
| The stack is up but not answering | a systemd timer that curls the site and restarts after 3 consecutive misses |
| The guest kernel panicked or hung | the emulated i6300esb watchdog, petted from inside the guest |

Two details specific to this stack:

- The app **waits on Postgres being healthy**, not merely started
  (`condition: service_healthy`). Linkwarden runs migrations on boot and exits
  if the database will not answer yet, so without the health gate a cold start
  is a restart loop that eventually settles and looks like nothing happened.
- The self-heal timer runs every 5 minutes rather than the vault's 2. Four
  containers and a database take real time to come back, and a check that
  fires during a legitimate restart is how a self-heal timer becomes the
  outage it was meant to prevent.

## Turning it off

⚠️ Setting `links: false` **destroys VM 998 on the next run of the hypervisor
play**, disk and bookmarks included. Run `--check --diff` first if you want to
see it coming.

Backups on `tank` outlive the guest, but restoring one is a manual job. If you
want the data in a format you can read without Proxmox, export it from
Linkwarden first (Settings → Import & Export).

Retire the credentials too: delete the five GitHub secrets, and revoke the
Cloudflare token at its source — it is a DNS-write credential for your whole
zone and nothing else uses it.

## Troubleshooting

**No certificate.** Almost always the Cloudflare token's scope — it needs the
"Edit zone DNS" template (Zone:Read **and** DNS:Edit) on the zone that owns
`conway-hash.com`:

```bash
ssh ci-deploy@links.ts.conway-hash.com 'sudo docker logs linkwarden-caddy 2>&1 | tail -50'
```

**It answers, then stops, then answers again.** The self-heal timer doing its
job — and a sign something underneath it is unhealthy:

```bash
ssh ci-deploy@links.ts.conway-hash.com 'journalctl -u linkwarden-healthcheck.service --since -1d'
```

**Login redirects somewhere wrong.** `NEXTAUTH_URL` must match the origin the
browser actually types. A mismatch does not fail loudly; it redirects to the
wrong host after login and looks like a broken session.

**Archiving fails on big pages.** The browser worker is memory-hungry. Check
whether the guest is being OOM-killed before assuming the app is at fault:

```bash
ssh ci-deploy@links.ts.conway-hash.com 'dmesg -T | grep -i "out of memory" | tail'
```
