#!/usr/bin/env bash
# install.sh — installs agent-sandbox to ~/.agent-sandbox (or $AGENT_SANDBOX_HOME)
set -euo pipefail

INSTALL_DIR="${AGENT_SANDBOX_HOME:-$HOME/.agent-sandbox}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing agent-sandbox to $INSTALL_DIR"

if [ -d "$INSTALL_DIR" ]; then
    echo "    Updating existing installation (presets and secrets are preserved)."
    # Sync everything except user-owned config
    rsync -a \
        --exclude='presets/' \
        --exclude='secrets/' \
        --exclude='.git/' \
        "$SCRIPT_DIR/" "$INSTALL_DIR/"
    # Only copy presets if they haven't been customized
    if [ ! -f "$INSTALL_DIR/presets/default/proxy.yaml" ]; then
        rsync -a "$SCRIPT_DIR/presets/" "$INSTALL_DIR/presets/"
    else
        echo "    Skipping presets/ (already customized)."
    fi
else
    rsync -a --exclude='.git/' "$SCRIPT_DIR/" "$INSTALL_DIR/"
fi

mkdir -p "$INSTALL_DIR/secrets"
chmod +x "$INSTALL_DIR/bin/sandbox"

# Add bin/ to PATH in shell rc
BIN_DIR="$INSTALL_DIR/bin"
SHELL_RC=""
case "${SHELL:-}" in
    */bash) SHELL_RC="$HOME/.bashrc" ;;
    */zsh)  SHELL_RC="$HOME/.zshrc"  ;;
esac

echo ""
echo "==> Installation complete."
echo ""
echo "Next steps:"
echo "  1. Configure a provider:  sandbox preset edit"
echo "  2. Set your API key:      export ANTHROPIC_API_KEY=sk-ant-..."
echo "  3. Build images (once):   sandbox build"
echo "  4. Start a project:       cd your-project && sandbox run --name <name>"
