#!/bin/sh
set -eu

repository="${FAILURE_MEMORY_REPOSITORY:-CongBao/failure-memory}"
base_url="${FAILURE_MEMORY_RELEASE_BASE_URL:-https://github.com/${repository}/releases/latest/download}"

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
"${temporary}/failure-memory" install runtime

runtime="${HOME}/.local/bin/failure-memory"
printf 'Installed Failure Memory at %s\n' "$runtime"
printf 'If your agent was already open, restart it so the plugin can find the command.\n'
