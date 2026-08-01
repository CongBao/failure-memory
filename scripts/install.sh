#!/bin/sh
set -eu

repository="${FAILURE_MEMORY_REPOSITORY:-CongBao/failure-memory}"
base_url="${FAILURE_MEMORY_RELEASE_BASE_URL:-https://github.com/${repository}/releases/latest/download}"
harness="${FAILURE_MEMORY_HARNESS:-auto}"
runtime_only=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --harness)
      if [ "$#" -lt 2 ]; then
        echo "failure-memory: --harness requires a value" >&2
        exit 2
      fi
      harness="$2"
      shift 2
      ;;
    --runtime-only)
      runtime_only=true
      shift
      ;;
    --help|-h)
      echo "usage: install.sh [--harness codex,claude,copilot,cursor|auto] [--runtime-only]"
      exit 0
      ;;
    *)
      echo "failure-memory: unknown installer option: $1" >&2
      exit 2
      ;;
  esac
done

case "$(uname -s)" in
  Darwin) platform="darwin" ;;
  Linux) platform="linux" ;;
  *) echo "failure-memory: unsupported operating system" >&2; exit 1 ;;
esac

case "$(uname -m)" in
  arm64|aarch64) architecture="arm64" ;;
  x86_64|amd64) architecture="amd64" ;;
  *) echo "failure-memory: unsupported CPU architecture" >&2; exit 1 ;;
esac

archive="failure-memory_${platform}_${architecture}.tar.gz"
temporary="$(mktemp -d "${TMPDIR:-/tmp}/failure-memory-install.XXXXXX")"
trap 'rm -rf "$temporary"' EXIT HUP INT TERM

curl --fail --location --proto '=https' --tlsv1.2 \
  "${base_url}/${archive}" --output "${temporary}/${archive}"
curl --fail --location --proto '=https' --tlsv1.2 \
  "${base_url}/checksums.txt" --output "${temporary}/checksums.txt"

expected="$(awk -v asset="$archive" '$2 == asset { print $1 }' "${temporary}/checksums.txt")"
if [ -z "$expected" ]; then
  echo "failure-memory: release checksum is missing" >&2
  exit 1
fi
actual="$(shasum -a 256 "${temporary}/${archive}" | awk '{ print $1 }')"
if [ "$actual" != "$expected" ]; then
  echo "failure-memory: release checksum verification failed" >&2
  exit 1
fi

tar -xzf "${temporary}/${archive}" -C "$temporary"
if [ "$runtime_only" = true ]; then
  "${temporary}/failure-memory" install runtime
else
  "${temporary}/failure-memory" install all --harness "$harness"
fi

runtime="${HOME}/.local/bin/failure-memory"
printf 'Installed Failure Memory at %s\n' "$runtime"
if [ "$runtime_only" = false ]; then
  printf 'Installed or updated plugins for: %s\n' "$harness"
fi
printf 'Restart any agent application that was already open.\n'
