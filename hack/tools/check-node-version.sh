#!/usr/bin/env bash
# The frontend's devDependencies (jsdom 30 and its transitive tree) refuse to
# install below NODE_MIN_VERSION. Left unchecked, an older host Node either
# fails `npm ci` with an opaque EBADENGINE dump from whichever nested package
# happens to be resolved first, or -- if a caller bypasses that with
# --ignore-engines/--force -- installs a native binding for the wrong Node
# ABI and fails much later with a confusing "Cannot find module" error deep
# inside a build. Running this before npm turns both failure modes into one
# actionable message.
set -Eeuo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=versions.env
source "$here/versions.env"

die() {
  printf 'frontend-check: %s\n' "$1" >&2
  exit 1
}

command -v node >/dev/null 2>&1 || die "node is not on PATH; install Node ${NODE_VERSION} (see frontend/.nvmrc) before running npm"

current="$(node --version)"
current_numeric="${current#v}"

# Plain dotted-decimal comparison is sufficient here: Node's LTS line only
# ever increments the even majors this repo supports (22, 24, 26, ...), so
# there is no earlier-major-but-numerically-larger version to misclassify.
lowest="$(printf '%s\n%s\n' "$NODE_MIN_VERSION" "$current_numeric" | sort -V | head -n1)"
if [ "$lowest" != "$NODE_MIN_VERSION" ]; then
  die "wrong Node version, expected v${NODE_VERSION}, found ${current}"
fi
