#!/bin/sh
# Deploy the project runner. This is the whole deployment: the service runs
# runner/supervisor.py from this checkout, so the checkout is what is live.
#
#   ./deploy.sh            install agents/rules, record, restart, verify
#   ./deploy.sh --check    non-zero when what runs differs from the checkout
#   ./deploy.sh --status   what is running, as JSON
#
# Merging a pull request does not deploy. Deploying does not require a merge.
set -eu
R=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$R/runner/deploy.py" "$@"
