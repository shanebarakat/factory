#!/usr/bin/env bash
# modal_exec.sh <command> [args...] — run one heavy command (a test suite, a mutant's test run)
# on a Modal Sandbox against the current directory and return its exit code. A drop-in prefix:
# `receipt.sh harness/modal_exec.sh <cmd>`; mutate.sh and flake.sh add it themselves when
# FACTORY_REMOTE_EXEC=modal. See modal_exec.py for the provenance digest and the options.
#
# Not for commands whose writes to the tree matter afterwards (a build, a codegen): nothing
# the command writes comes back.
set -uo pipefail
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PY="${FACTORY_MODAL_PYTHON:-$D/../.modal-venv/bin/python}"
[ -x "$PY" ] || {
  echo "modal-exec: no Modal client at $PY; run: uv venv --python 3.12 .modal-venv && uv pip install --python .modal-venv/bin/python modal" >&2
  exit 69
}
DOPPLER="${FACTORY_DOPPLER:-doppler}"
if [ -z "${FACTORY_PACKAGES_TOKEN:-}" ]; then
  FACTORY_PACKAGES_TOKEN="$("$DOPPLER" secrets get GH_PACKAGES_READ_TOKEN --plain \
    --project wander-os --config dev 2>/dev/null || true)"
fi
export FACTORY_PACKAGES_TOKEN
if [ -n "${MODAL_TOKEN_ID:-}" ] && [ -n "${MODAL_TOKEN_SECRET:-}" ]; then
  exec "$PY" -I "$D/modal_exec.py" "$@"
fi
exec "$DOPPLER" run --project factory --config dev \
  --only-secrets MODAL_TOKEN_ID,MODAL_TOKEN_SECRET -- "$PY" -I "$D/modal_exec.py" "$@"
