#!/bin/sh
# Runs every test from a clean start.
# The first two commands install the test packages and need network access once.
# Set SKIP_INSTALL=1 to skip them when the packages are already there.
# Any folders given as arguments are scanned as ordinary images.
set -e
cd "$(dirname "$0")/.."
if [ -z "$SKIP_INSTALL" ]; then
  python3 -m pip install -r tests/requirements.txt
  python3 -m playwright install chromium
fi
sh tests/make_certs.sh
python3 tests/make_test_files.py
python3 tests/run_tests.py "$@"
