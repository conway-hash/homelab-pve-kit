#!/usr/bin/env bash
#
# Smoke test — watch.
#
# See tests/vault.sh for the contract every tests/<service>.sh follows.

set -uo pipefail

# A cold tailnet path is not a fault.
#
# Tailscale drops a direct path between peers that have not talked for a while
# and renegotiates on the next packet, falling back to a DERP relay while it
# does. Measured on the idle ntfy guest: first request 8s+ and timing out,
# second 4.4s, third 0.12s. The busy guests never show it because their paths
# never go cold — which is why this only ever bit the one service nothing has
# started using yet.
#
# So the first reachability check warms the path and retries, and only then
# decides. Everything after it runs on an established path and needs no retry.
curl_warm() {
  local url="$1" i out
  for i in 1 2 3; do
    if out=$(curl -fsS --max-time 20 "$url" 2>&1); then
      printf '%s' "$out"
      return 0
    fi
    sleep 3
  done
  printf '%s' "$out"
  return 1
}

FAILED=0

BASE=$(grep '^tailnet_base_domain:' group_vars/all/vars.yml \
  | sed 's/^tailnet_base_domain:[[:space:]]*//; s/[[:space:]]*#.*$//')
DOMAIN="watch.${BASE}"

SSH="ssh -i $HOME/.ssh/homelab_ci_deploy -o StrictHostKeyChecking=accept-new"

if curl_warm "https://${DOMAIN}/healthz" >/dev/null; then
  echo "OK: ${DOMAIN} answers over verified HTTPS"
else
  echo "::error::${DOMAIN}/healthz did not answer over verified HTTPS"
  FAILED=1
fi

# ⚠️ /healthz deliberately does NOT depend on Proxmox, so it passes against a
# watchd that is serving and blind. This is the check that proves the token
# works — and a blind monitor is the exact failure that would otherwise sit
# there looking green while telling you nothing.
if ! STATE=$(curl -fsS --max-time 30 "https://${DOMAIN}/api/state?since=0" 2>&1); then
  echo "::error::could not read ${DOMAIN}/api/state (${STATE}) — the dashboard is serving but has no data"
  FAILED=1
else
  ERRORS=$(printf '%s' "$STATE" | python3 -c \
    'import json,sys; print("; ".join(json.load(sys.stdin)["latest"].get("errors") or []))' 2>/dev/null)
  if [ -n "$ERRORS" ]; then
    echo "::error::watchd cannot read the hypervisor: ${ERRORS} — check the Proxmox token's role, see docs/watch.md"
    FAILED=1
  else
    echo "OK: watchd is reading the hypervisor cleanly"
  fi

  # A dashboard reporting zero guests is not "all clear", it is a broken read
  # that looks like good news. The vault alone guarantees at least one.
  GUESTS=$(printf '%s' "$STATE" | python3 -c \
    'import json,sys; print(len(json.load(sys.stdin)["latest"].get("guests") or []))' 2>/dev/null)
  if [ "${GUESTS:-0}" -ge 1 ]; then
    echo "OK: ${GUESTS} guest(s) visible"
  else
    echo "::error::watchd sees no guests at all, which cannot be true while the vault is running"
    FAILED=1
  fi
fi

# The dashboard's buttons run `apt-get dist-upgrade` and `systemctl reboot` on
# the hypervisor, so the guard in front of them is worth asserting.
#
# A cross-origin form post cannot set a custom header, so requiring X-Kiosk is
# what stops any page you happen to open from posting here on your behalf.
# Without the header it must be refused, whatever kiosk_control_reach is set to.
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
  -X POST -H 'Content-Type: application/json' \
  -d '{"action":"reboot"}' "https://${DOMAIN}/api/do")
if [ "$CODE" = "403" ] || [ "$CODE" = "400" ] || [ "$CODE" = "404" ]; then
  echo "OK: a request without the X-Kiosk header is refused (HTTP $CODE)"
else
  echo "::error::POST /api/do without X-Kiosk returned HTTP ${CODE}, expected 403 — any page you open could reboot the hypervisor"
  FAILED=1
fi

# And the page itself is the kiosk, proxied. If this stops being true the
# dashboard silently becomes something else.
# Captured, not piped into `grep -q`.
#
# grep -q exits the moment it matches, which closes the pipe under curl — curl
# then dies with EPIPE and exit 23 while having done nothing wrong. The `if`
# reads that as a failure, so the check fails precisely BECAUSE the page it is
# looking for was found. Buffering the body first removes the race entirely.
PAGE=$(curl -fsS --max-time 20 "https://${DOMAIN}/" || true)
if printf '%s' "$PAGE" | grep -q 'id="tnwrap"'; then
  echo "OK: the dashboard is the kiosk page"
else
  echo "::error::https://${DOMAIN}/ is not serving the kiosk page — the proxy to the hypervisor is broken"
  FAILED=1
fi

# 443 must NOT be on 0.0.0.0 — and behind this one are the reboot endpoints.
# ssh's exit status checked separately; see tests/vault.sh.
if ! PORTS=$($SSH ci-deploy@"${DOMAIN}" 'sudo docker port watch-caddy' 2>&1); then
  echo "::error::could not read watch-caddy's published ports (${PORTS}) — cannot prove 443 is tailnet-only"
  FAILED=1
elif printf '%s' "$PORTS" | grep -q '0\.0\.0\.0:443'; then
  echo "::error::watch-caddy publishes 443 on 0.0.0.0 — the dashboard AND its reboot controls are exposed to the whole LAN"
  FAILED=1
else
  echo "OK: 443 is not published on 0.0.0.0"
fi

if $SSH ci-deploy@"${DOMAIN}" 'systemctl is-active --quiet watch-healthcheck.timer'; then
  echo "OK: self-heal timer is active"
else
  echo "::error::watch-healthcheck.timer is not active — a wedged stack would never be restarted"
  FAILED=1
fi

exit $FAILED
