#!/usr/bin/env bash
#
# Smoke test — ntfy.
#
# See tests/vault.sh for the contract every tests/<service>.sh follows: cwd is
# ansible/, the ci-deploy key is at ~/.ssh/homelab_ci_deploy, the runner is on
# the tailnet, and every check has a FAILING branch.

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

DOMAIN="ntfy.$(grep '^tailnet_base_domain:' group_vars/all/vars.yml \
  | sed 's/^tailnet_base_domain:[[:space:]]*//; s/[[:space:]]*#.*$//')"

SSH="ssh -i $HOME/.ssh/homelab_ci_deploy -o StrictHostKeyChecking=accept-new"

# The check the Android app makes: MagicDNS resolves the name, Caddy answers
# with a certificate that verifies against the public trust store, and ntfy is
# awake behind it. No -k anywhere — the app refuses a certificate it cannot
# verify, so an unverifiable one has to fail here too.
if curl_warm "https://${DOMAIN}/v1/health" | grep -q '"healthy":true'; then
  echo "OK: ${DOMAIN} answers over verified HTTPS"
else
  echo "::error::${DOMAIN}/v1/health did not report healthy over verified HTTPS — no alert from any service can be delivered"
  FAILED=1
fi

# ⚠️ The check that matters most on this guest.
#
# ntfy's DEFAULT is read-write for anonymous users: every topic world-readable
# and world-writable to anything that can reach it. If NTFY_AUTH_DEFAULT_ACCESS
# ever stops being applied, nothing else here would notice — the server would
# be perfectly healthy and completely open. Anonymous publish must be REFUSED.
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
  -d "smoke test: this must never be accepted" "https://${DOMAIN}/watch")
if [ "$CODE" = "401" ] || [ "$CODE" = "403" ]; then
  echo "OK: anonymous publish is refused (HTTP $CODE)"
else
  echo "::error::anonymous publish to ${DOMAIN}/watch returned HTTP ${CODE}, expected 401/403 — the notification server is open to anything on the tailnet"
  FAILED=1
fi

# Anonymous READ must be refused too, or anything on the tailnet can watch
# every alert this homelab produces.
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
  "https://${DOMAIN}/watch/json?poll=1")
if [ "$CODE" = "401" ] || [ "$CODE" = "403" ]; then
  echo "OK: anonymous read is refused (HTTP $CODE)"
else
  echo "::error::anonymous read of ${DOMAIN}/watch returned HTTP ${CODE}, expected 401/403"
  FAILED=1
fi

# 443 must NOT be on 0.0.0.0. The ssh exit status is checked SEPARATELY from
# the grep: piping into `grep -q` and treating no-match as success means an
# unreachable host produces empty output, no match, and a cheerful OK — a check
# that passes precisely when things are most broken.
if ! PORTS=$($SSH ci-deploy@"${DOMAIN}" 'sudo docker port ntfy-caddy' 2>&1); then
  echo "::error::could not read ntfy-caddy's published ports (${PORTS}) — cannot prove 443 is tailnet-only"
  FAILED=1
elif printf '%s' "$PORTS" | grep -q '0\.0\.0\.0:443'; then
  echo "::error::ntfy-caddy publishes 443 on 0.0.0.0 — the notification server is exposed to the whole LAN"
  FAILED=1
else
  echo "OK: 443 is not published on 0.0.0.0"
fi

# The self-heal timer is the only thing covering "running but not answering".
if $SSH ci-deploy@"${DOMAIN}" 'systemctl is-active --quiet ntfy-healthcheck.timer'; then
  echo "OK: self-heal timer is active"
else
  echo "::error::ntfy-healthcheck.timer is not active — a wedged stack would never be restarted"
  FAILED=1
fi

exit $FAILED
