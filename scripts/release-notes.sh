#!/bin/sh

set -eu

version="${1:-}"
changelog="${2:-CHANGELOG.md}"

if [ -z "$version" ]; then
  echo "usage: $0 VERSION [CHANGELOG]" >&2
  exit 2
fi

awk -v version="$version" '
  index($0, "## [" version "]") == 1 {
    found = 1
    capture = 1
    next
  }
  capture && /^## \[/ {
    exit
  }
  capture {
    print
  }
  END {
    if (!found) {
      exit 1
    }
  }
' "$changelog"
