#!/bin/sh
set -eu

minimum='import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'

if [ "${WECHAT_PIPELINE_PYTHON:-}" ]; then
  if "${WECHAT_PIPELINE_PYTHON}" -c "$minimum" >/dev/null 2>&1; then
    exec "${WECHAT_PIPELINE_PYTHON}" "$@"
  fi
  echo "error: WECHAT_PIPELINE_PYTHON is not a usable Python 3.10+ executable" >&2
  exit 1
fi

for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "$minimum" >/dev/null 2>&1; then
    exec "$candidate" "$@"
  fi
done

echo "error: Python 3.10 or newer was not found; install it or set WECHAT_PIPELINE_PYTHON" >&2
exit 1
