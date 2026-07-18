#!/bin/sh
set -eu

minimum='import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'

# Read config file before scanning PATH — this ensures the configured Python
# path is honored even when the parent process (e.g. AAMP Feishu bridge) runs
# with a limited launchd PATH that doesn't include Homebrew.
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
CONFIG_FILE="$XDG_CONFIG_HOME/wechat-pipeline/.env"
if [ -r "$CONFIG_FILE" ]; then
  while IFS='=' read -r key value || [ -n "$key" ]; do
    # Skip comments and empty lines
    case "$key" in
      \#*) continue ;;
      '') continue ;;
    esac
    # Remove leading/trailing whitespace and quotes
    key=$(printf '%s' "$key" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    value=$(printf '%s' "$value" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;s/^["'\'']//;s/["'\'']$//')
    if [ "$key" = "WECHAT_PIPELINE_PYTHON" ] && [ -n "$value" ]; then
      if [ -x "$value" ] && "$value" -c "$minimum" >/dev/null 2>&1; then
        exec "$value" "$@"
      fi
      echo "error: WECHAT_PIPELINE_PYTHON=$value is not a usable Python 3.10+ executable" >&2
      exit 1
    fi
  done <"$CONFIG_FILE"
fi

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
