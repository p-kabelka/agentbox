#!/bin/bash
set -euo pipefail

# Start GCE metadata server if vertex.metadata_server is enabled in proxy.yaml
if python3 -c "
import sys, yaml
cfg = yaml.safe_load(open('/config/proxy.yaml'))
v = next((p for p in cfg.get('providers', []) if p.get('name') == 'vertex'), {})
sys.exit(0 if v.get('enabled') and v.get('metadata_server') else 1)
" 2>/dev/null; then
    python3 /app/metadata_server.py &
    META_PID=$!
fi

mitmweb --listen-host 0.0.0.0 --listen-port 8080 \
        --web-host 0.0.0.0 --web-port 8081 \
        --scripts /addons/injector.py --scripts /addons/logger.py \
        --set block_global=false \
        --set web_password= \
        --no-web-open-browser &
MITM_PID=$!

until [ -f "$HOME/.mitmproxy/mitmproxy-ca-cert.pem" ]; do sleep 1; done

trap 'kill "${MITM_PID}" "${META_PID:-}" 2>/dev/null; exit 0' TERM INT
if [ -n "${META_PID:-}" ]; then
    wait -n "${MITM_PID}" "${META_PID}"
else
    wait "${MITM_PID}"
fi
EXIT_CODE=$?
kill "${MITM_PID}" "${META_PID:-}" 2>/dev/null
exit "$EXIT_CODE"
