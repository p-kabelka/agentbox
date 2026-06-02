#!/bin/bash
set -euo pipefail

# Trust only the mitmproxy CA — the agent has no direct internet access,
# all TLS is terminated at the proxy which re-signs with this CA.
CA=/proxy-ca/mitmproxy-ca-cert.pem
until [ -f "$CA" ]; do sleep 0.2; done
export SSL_CERT_FILE="$CA"
export CURL_CA_BUNDLE="$CA"
export GIT_SSL_CAINFO="$CA"
export REQUESTS_CA_BUNDLE="$CA"
export NODE_EXTRA_CA_CERTS="$CA"

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

# krun's virtio-console sends \n for Enter instead of \r. Node.js readline in
# raw mode expects \r. setRawMode() clears icrnl but not inlcr, so inlcr set
# here survives the raw-mode transition and translates \n back to \r.
stty inlcr 2>/dev/null || true

if [ $# -gt 0 ]; then
    exec "$@"
elif [ -n "${AGENT_HARNESS:-}" ]; then
    echo "[agent] Starting ${AGENT_HARNESS} ${AGENT_HARNESS_ARGS:-}..."
    exec ${AGENT_HARNESS} ${AGENT_HARNESS_ARGS:-}
else
    exec /bin/bash
fi
