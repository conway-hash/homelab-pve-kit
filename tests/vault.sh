#!/usr/bin/env bash
#
# Smoke test — vault (Vaultwarden).
#
# Run by deploy.yml after the playbook, and runnable by hand:
#
#   tests/vault.sh
#
# The contract every tests/<service>.sh follows:
#
#   * cwd is ansible/, so group_vars paths resolve the same way the playbook
#     sees them
#   * the ci-deploy key is at ~/.ssh/homelab_ci_deploy and the runner is on
#     the tailnet
#   * exit non-zero on failure, and print ::error:: so the line surfaces in
#     the GitHub Actions log
#
# A test whose every path ends in exit 0 is not a test. This repo has shipped
# a false-green run that way before, which is why each check below has a
# failing branch and FAILED is only ever set, never reset.

set -uo pipefail

FAILED=0

DOMAIN="vault.$(grep '^tailnet_base_domain:' group_vars/all/vars.yml \
  | sed 's/^tailnet_base_domain:[[:space:]]*//; s/[[:space:]]*#.*$//')"

SSH="ssh -i $HOME/.ssh/homelab_ci_deploy -o StrictHostKeyChecking=accept-new"

# The check a real client makes, from off-box, over the tailnet: MagicDNS
# resolves the name, Caddy answers with a certificate that verifies against the
# public trust store, and Vaultwarden is awake behind it. No -k anywhere — an
# unverifiable certificate is one of the two outages this is here to catch, so
# it has to fail.
if curl -fsS --max-time 30 "https://${DOMAIN}/alive" >/dev/null; then
  echo "OK: ${DOMAIN} answers over verified HTTPS"
else
  echo "::error::${DOMAIN}/alive did not answer over verified HTTPS — the vault is unreachable by every Bitwarden client"
  FAILED=1
fi

# Registration being open would let anything that reaches the tailnet create an
# account on the vault server.
#
# Read from the RUNNING container, not from /api/config: Vaultwarden stopped
# exposing the signups flag there, so the previous version of this check quietly
# degraded to a warning and verified nothing. A check that cannot fail is worse
# than no check, because it reads like coverage.
SIGNUPS=$($SSH ci-deploy@"${DOMAIN}" \
  'sudo docker exec vaultwarden printenv SIGNUPS_ALLOWED' 2>/dev/null | tr -d '\r')
if [ "$SIGNUPS" = "false" ]; then
  echo "OK: signups are closed"
else
  echo "::error::SIGNUPS_ALLOWED is '${SIGNUPS:-unreadable}', expected 'false' — anyone on the tailnet can register on your vault"
  FAILED=1
fi

# 443 must NOT be listening on 0.0.0.0. This is the bug the vault shipped with:
# a bare "443:443" published the service to every device on the home LAN while
# every doc claimed tailnet-only.
#
# The ssh exit status is checked SEPARATELY from the grep, and that is the whole
# point of the next four lines. Piping straight into `grep -q` and treating
# no-match as success means an unreachable host — wrong name, dead guest, broken
# tailnet — produces empty output, no match, and a cheerful "OK". That is a
# check that passes precisely when things are most broken.
if ! PORTS=$($SSH ci-deploy@"${DOMAIN}" 'sudo docker port vaultwarden-caddy' 2>&1); then
  echo "::error::could not read vaultwarden-caddy's published ports (${PORTS}) — cannot prove 443 is tailnet-only"
  FAILED=1
elif printf '%s' "$PORTS" | grep -q '0\.0\.0\.0:443'; then
  echo "::error::vaultwarden-caddy publishes 443 on 0.0.0.0 — the vault is exposed to the whole LAN, not just the tailnet"
  FAILED=1
else
  echo "OK: 443 is not published on 0.0.0.0"
fi

# The self-heal timer is the only thing covering "running but not answering".
# If it is not armed, that failure mode is uncovered and nothing else here
# would notice.
#
# Straight to the tailnet name, not through the hypervisor. The inventory jumps
# via pve so a guest with a broken tailnet join is still fixable; a smoke test
# wants the opposite — if the vault is not reachable the way a client reaches
# it, that is a failure, not something to route around.
if $SSH ci-deploy@"${DOMAIN}" \
  'systemctl is-active --quiet vaultwarden-healthcheck.timer'; then
  echo "OK: self-heal timer is active"
else
  echo "::error::vaultwarden-healthcheck.timer is not active — a wedged stack would never be restarted"
  FAILED=1
fi

exit $FAILED
