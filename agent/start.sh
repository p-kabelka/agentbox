#!/bin/bash
set -euo pipefail

# passt disables DHCP on gateway-less internal networks. Restore only the
# allocated address and connected subnet route before contacting either sidecar.
/usr/local/bin/agentbox-network

# Trust only the mitmproxy CA — the agent has no direct internet access,
# all TLS is terminated at the proxy which re-signs with this CA.
CA=/proxy-ca/mitmproxy-ca-cert.pem
# wait until the proxy certificate is created by proxy container
until [ -f "$CA" ]; do sleep 0.2; done
ANCHOR=/etc/pki/ca-trust/source/anchors/proxy-ca.crt
if [ ! -f "$ANCHOR" ] || [ "$(<"$CA")" != "$(<"$ANCHOR")" ]; then
    # append it to system cert store (faster than update-ca-trust)
    cat "$CA" >> /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
    cp "$CA" "$ANCHOR"
fi
# make libraries trust the cert directly
export SSL_CERT_FILE="$CA"
export CURL_CA_BUNDLE="$CA"
export GIT_SSL_CAINFO="$CA"
export REQUESTS_CA_BUNDLE="$CA"
export NODE_EXTRA_CA_CERTS="$CA"

# Replace image defaults (including .bashrc) on first boot, then preserve edits.
if [ -d /agentbox-dotfiles ] && [ ! -e "$HOME/.agentbox-dotfiles-seeded" ]; then
    cp -rT /agentbox-dotfiles "$HOME"
    touch "$HOME/.agentbox-dotfiles-seeded"
fi

# Clone current shares once. In a running sandbox, use agentbox-clone NAME for
# repositories shared later. Existing working trees are always left untouched.
/usr/local/bin/agentbox-clone --wait 80

# krun's virtio-console sends \n for Enter instead of \r. Node.js readline in
# raw mode expects \r. setRawMode() clears icrnl but not inlcr, so inlcr set
# here survives the raw-mode transition and translates \n back to \r.
# DEPRECATED: not required after libkrun 1.18.0: https://github.com/containers/libkrun/issues/562
# stty inlcr 2>/dev/null || true

if [ $# -gt 0 ]; then
    exec "$@"
elif [ -n "${AGENT_HARNESS:-}" ]; then
    echo "[agent] Starting ${AGENT_HARNESS} ${AGENT_HARNESS_ARGS:-}..."
    exec ${AGENT_HARNESS} ${AGENT_HARNESS_ARGS:-}
else
    exec /bin/bash
fi
