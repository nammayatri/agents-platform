#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "=== Agent Platform CLI Installer ==="
echo ""

# Check Python version
PYTHON=""
for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Error: Python 3.11+ is required but not found."
    exit 1
fi

echo "Using Python: $PYTHON ($($PYTHON --version))"

# Create virtual environment
if [ -d "$VENV_DIR" ]; then
    echo "Virtual environment already exists, reinstalling..."
else
    echo "Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

# Install the package
echo "Installing agents-cli..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -e "$SCRIPT_DIR"

AGENTS_BIN="$VENV_DIR/bin/agents"

echo ""
echo "Installation complete!"
echo ""
echo "Usage options:"
echo "  1. Run directly:  $AGENTS_BIN --help"
echo "  2. Add to PATH:   export PATH=\"$VENV_DIR/bin:\$PATH\""
echo "  3. Create alias:  alias agents='$AGENTS_BIN'"
echo ""
echo "Quick start:"
echo "  agents config set-url http://localhost:8000"
echo "  agents login"
echo "  agents projects"
