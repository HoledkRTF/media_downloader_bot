#!/bin/bash
set -e

# Install python dependencies
pip install -r requirements.txt

# Explicitly export the path so it installs locally to the venv
export PLAYWRIGHT_BROWSERS_PATH=0

# Install Playwright chromium browser (without system deps to avoid sudo requirements)
python -m playwright install chromium
