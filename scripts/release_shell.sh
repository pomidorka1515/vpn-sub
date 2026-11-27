#!/usr/bin/env bash
# GitHub Actions custom shell: common build steps run in the selected libc.
# The host and musl container share the checkout, runner temp and ccache paths,
# so Actions-generated scripts and GITHUB_ENV work in both environments.
set -euo pipefail
# All steps after dependency installation use the locked build environment.
if [ -x "$PWD/venv/bin/python" ]; then
  export PATH="$PWD/venv/bin:$PATH"
fi
library_path="${LD_LIBRARY_PATH:-}"
if [ -n "${RELEASE_OPENSSL_LIB:-}" ]; then
  library_path="$RELEASE_OPENSSL_LIB${library_path:+:$library_path}"
fi
if [ "${RELEASE_LIBC:-glibc}" = musl ]; then
  exec docker exec \
    --env GITHUB_ENV --env LD_LIBRARY_PATH="$library_path" --env NUITKA_CCACHE_BINARY \
    --env UV_PROJECT_ENVIRONMENT=venv \
    --env PATH="$PWD/venv/bin:/usr/local/bin:/usr/bin:/bin" \
    vpn-sub-musl bash --noprofile --norc -e -o pipefail "$@"
fi
export LD_LIBRARY_PATH="$library_path"
exec bash --noprofile --norc -e -o pipefail "$@"
