#!/bin/bash
set -e

# Install python dependencies
pip install -r requirements.txt

# Install Playwright chromium browser with dependencies
playwright install --with-deps chromium
