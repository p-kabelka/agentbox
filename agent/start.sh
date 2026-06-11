#!/bin/bash
set -euo pipefail

# Trust only the mitmproxy CA — the agent has no direct internet access,
# all TLS is terminated at the proxy which re-signs with this CA.
CA=/proxy-ca/mitmproxy-ca-cert.pem
# wait until the proxy certificate is created by proxy container
until [ -f "$CA" ]; do sleep 0.2; done
# append it to system cert store (faster than update-ca-trust)
cat "$CA" >> /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
# copy it to system cert sources in case update-ca-trust runs
cp "$CA" /etc/pki/ca-trust/source/anchors/proxy-ca.crt
# make libraries trust the cert directly
export SSL_CERT_FILE="$CA"
export CURL_CA_BUNDLE="$CA"
export GIT_SSL_CAINFO="$CA"
export REQUESTS_CA_BUNDLE="$CA"
export NODE_EXTRA_CA_CERTS="$CA"

# Clone source bundle and wire up writable output remote
if [ -f /source/project.bundle ] && [ ! -d /workspace/.git ]; then
    echo "[agent] Cloning workspace..."
    git clone -q --no-local /source/project.bundle /workspace
    git -C /workspace branch --unset-upstream
    git -C /workspace remote rename origin source
    git -C /workspace remote add origin /output/repo.git
    # let git create and track the remote branch automatically when it does not exist
    git -C /workspace config push.autoSetupRemote true
    echo "[agent] When done: git push origin HEAD"
fi

# Apply preset dotfiles to home directory
[ -d /agentbox-dotfiles ] && cp -rT /agentbox-dotfiles ~/

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
