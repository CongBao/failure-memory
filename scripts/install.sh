#!/bin/sh
set -eu

repository="${FAILURE_MEMORY_REPOSITORY:-CongBao/failure-memory}"
base_url="${FAILURE_MEMORY_RELEASE_BASE_URL:-https://github.com/${repository}/releases/latest/download}"
harness="${FAILURE_MEMORY_HARNESS:-auto}"
requested_version="${FAILURE_MEMORY_VERSION:-latest}"
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
    --version)
      if [ "$#" -lt 2 ]; then
        echo "failure-memory: --version requires a value" >&2
        exit 2
      fi
      requested_version="$2"
      shift 2
      ;;
    --help|-h)
      echo "usage: install.sh [--harness codex,claude,copilot,cursor|auto] [--version vX.Y.Z|latest] [--runtime-only]"
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

if [ -z "${FAILURE_MEMORY_RELEASE_BASE_URL:-}" ]; then
  if [ "$requested_version" = "latest" ]; then
    base_url="https://github.com/${repository}/releases/latest/download"
  else
    case "$requested_version" in
      v[0-9]*.[0-9]*.[0-9]*|v[0-9]*.[0-9]*.[0-9]*-*) ;;
      *) echo "failure-memory: --version must be latest or a v-prefixed release" >&2; exit 2 ;;
    esac
    base_url="https://github.com/${repository}/releases/download/${requested_version}"
  fi
fi

case "$(uname -m)" in
  arm64|aarch64) architecture="arm64" ;;
  x86_64|amd64) architecture="amd64" ;;
  *) echo "failure-memory: unsupported CPU architecture" >&2; exit 1 ;;
esac

archive="failure-memory_${platform}_${architecture}.tar.gz"
temporary="$(mktemp -d "${TMPDIR:-/tmp}/failure-memory-install.XXXXXX")"
trap 'rm -rf "$temporary"' EXIT HUP INT TERM

download() {
  source_url="$1"
  destination_path="$2"
  if [ "${FAILURE_MEMORY_ALLOW_INSECURE_TEST_URL:-0}" = "1" ]; then
    curl --fail --location "$source_url" --output "$destination_path"
  else
    curl --fail --location --proto '=https' --tlsv1.2 \
      "$source_url" --output "$destination_path"
  fi
}

download "${base_url}/${archive}" "${temporary}/${archive}"
download "${base_url}/checksums.txt" "${temporary}/checksums.txt"

expected="$(awk -v asset="$archive" '$2 == asset { print $1 }' "${temporary}/checksums.txt")"
if [ -z "$expected" ]; then
  echo "failure-memory: release checksum is missing" >&2
  exit 1
fi
if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "${temporary}/${archive}" | awk '{ print $1 }')"
elif command -v shasum >/dev/null 2>&1; then
  actual="$(shasum -a 256 "${temporary}/${archive}" | awk '{ print $1 }')"
else
  echo "failure-memory: sha256sum or shasum is required to verify the release" >&2
  exit 1
fi
if [ "$actual" != "$expected" ]; then
  echo "failure-memory: release checksum verification failed" >&2
  exit 1
fi

tar -xzf "${temporary}/${archive}" -C "$temporary"
if [ "$requested_version" != "latest" ]; then
  installed_version="$("${temporary}/failure-memory" version | awk '{ print $2 }')"
  if [ "v${installed_version}" != "$requested_version" ]; then
    echo "failure-memory: downloaded runtime version does not match ${requested_version}" >&2
    exit 1
  fi
fi
if [ "$runtime_only" = true ]; then
  "${temporary}/failure-memory" install runtime
else
  "${temporary}/failure-memory" install all --harness "$harness"
fi

runtime="${FAILURE_MEMORY_RUNTIME_PATH:-${HOME}/.local/bin/failure-memory}"
printf 'Installed Failure Memory at %s\n' "$runtime"
if [ "$runtime_only" = false ]; then
  printf 'Installed or updated plugins for: %s\n' "$harness"
fi
printf 'Restart any agent application that was already open.\n'
