#!/bin/bash
set -euo pipefail

# Install mitmproxy CA cert into system trust store
CA=/proxy-ca/mitmproxy-ca-cert.pem
until [ -f "$CA" ]; do sleep 1; done
cp "$CA" /usr/local/share/ca-certificates/proxy-ca.crt
update-ca-certificates --fresh >/dev/null 2>&1

export NODE_EXTRA_CA_CERTS="$CA"
# Python requests / httpx
export REQUESTS_CA_BUNDLE="$CA"

# Resolve proxy hostname to IP — proxychains4 requires numeric addresses
PROXY_IP=$(getent hosts proxy | awk '{print $1; exit}')
printf "strict_chain\n\n[ProxyList]\nhttp %s 8080\n" "$PROXY_IP" > /etc/proxychains4.conf

# Clone source bundle and wire up read-only source + writable output remotes
if [ -f /source/project.bundle ] && [ ! -d /workspace/.git ]; then
    echo "[agent] Cloning workspace..."
    git clone -q /source/project.bundle /workspace
    git -C /workspace remote rename origin source
    git -C /workspace remote add output /output/repo.git
fi

[ "${1:-}" = "--shell" ] && exec /bin/bash

echo "[agent] Starting ${AGENT_HARNESS:-claude}..."
exec proxychains4 -q "${AGENT_HARNESS:-claude}" "$@"
