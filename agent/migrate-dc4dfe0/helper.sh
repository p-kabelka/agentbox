#!/bin/bash
set -euo pipefail
tools=/.agentbox-dc4dfe0
name=${0##*/}
if [[ $name == ip ]]; then
    exec "$tools/ld.so" --library-path "$tools/lib64" "$tools/bin/ip" "$@"
fi
# Only the network helper needs the private ip on PATH; do not expose it to
# the user command or change how Git finds its own utilities.
if [[ $name == agentbox-network ]]; then
    export PATH="$tools/shims:$PATH"
fi
exec "$tools/ld.so" --library-path "$tools/lib64" \
    "$tools/bin/python3" -I "$tools/$name.py" "$@"
