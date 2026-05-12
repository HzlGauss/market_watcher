#!/bin/bash
cd "$(dirname "$0")"

echo "======================================================="
echo "      Stock Market Monitoring Radar v2"
echo "      Standalone Version - Modular Refactor"
echo "======================================================="
echo ""

PYTHON_CMD=""
command -v python3 &>/dev/null && PYTHON_CMD="python3" || { command -v python &>/dev/null && PYTHON_CMD="python"; }
[ -z "$PYTHON_CMD" ] && { echo "Python not found"; exit 1; }

echo "Using $($PYTHON_CMD --version)"

$PYTHON_CMD -c "import requests" 2>/dev/null || { echo "Installing requests..."; pip3 install requests -q; }
echo ""
echo "Auto-scan every 15 minutes, push alerts to WeChat"
echo "Press Ctrl+C to stop"
echo ""

PYTHONPATH="$(pwd):$PYTHONPATH" $PYTHON_CMD __main__.py
