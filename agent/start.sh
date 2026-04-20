#!/bin/bash
set -euo pipefail

# Install mitmproxy CA cert into system trust store
CA=/proxy-ca/mitmproxy-ca-cert.pem
until [ -f "$CA" ]; do sleep 1; done
cp "$CA" /etc/pki/ca-trust/source/anchors/proxy-ca.crt
update-ca-trust extract >/dev/null 2>&1
export NODE_EXTRA_CA_CERTS="$CA"
export REQUESTS_CA_BUNDLE="$CA"

# Clone source bundle and wire up read-only source + writable output remotes
if [ -f /source/project.bundle ] && [ ! -d /workspace/.git ]; then
    echo "[agent] Cloning workspace..."
    git clone -q /source/project.bundle /workspace
    git -C /workspace remote rename origin source
    git -C /workspace remote add output /output/repo.git
    echo "[agent] When done: git push output HEAD"
fi

# Apply preset dotfiles to home directory
[ -d /agentbox-dotfiles ] && cp -rT /agentbox-dotfiles ~/

[ "${1:-}" = "--shell" ] && exec /bin/bash

# krun's virtio-console sends \n for Enter instead of \r. Node.js readline in
# raw mode expects \r. setRawMode() clears icrnl but not inlcr, so inlcr set
# here survives the raw-mode transition and translates \n back to \r.
stty inlcr 2>/dev/null || true

if [ -z "${AGENT_HARNESS:-}" ]; then
    echo "[agent] No AGENT_HARNESS set, starting bash."
    exec /bin/bash
fi

echo "[agent] Starting ${AGENT_HARNESS} ${AGENT_HARNESS_ARGS:-}..."
exec ${AGENT_HARNESS} ${AGENT_HARNESS_ARGS:-} "$@"
