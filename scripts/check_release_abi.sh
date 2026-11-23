#!/usr/bin/env bash
# Audit the bootstrap AND every ELF in the unpacked payload. musl must remain
# a host dependency, not a copied loader/libc hidden in the onefile archive.
set -euo pipefail
binary="$1"
libc="$2"
case "$binary" in
  *-amd64-*) machine='Advanced Micro Devices X86-64'; loader_arch=x86_64 ;;
  *-aarch64-*) machine=AArch64; loader_arch=aarch64 ;;
  *) echo "Unknown release architecture: $binary" >&2; exit 1 ;;
esac
case "$libc:$loader_arch" in
  musl:*) loader="/lib/ld-musl-$loader_arch.so.1" ;;
  glibc:x86_64) loader=/lib64/ld-linux-x86-64.so.2 ;;
  glibc:aarch64) loader=/lib/ld-linux-aarch64.so.1 ;;
  *) echo "Unknown libc: $libc" >&2; exit 1 ;;
esac
readelf -h "$binary" | grep -F "Machine:" | grep -F "$machine"
readelf -l "$binary" | grep -F "Requesting program interpreter: $loader]"
shopt -s nullglob
payloads=("$(dirname "$binary")"/*.dist)
test "${#payloads[@]}" -eq 1
while IFS= read -r -d '' path; do
  case "$(basename "$path")" in
    ld-musl-*|libc.musl-*|libc.so*|ld-linux-*)
      echo "Unexpected bundled libc/loader: $path" >&2; exit 1 ;;
  esac
  if ! header="$(readelf -h "$path" 2>/dev/null)"; then continue; fi
  grep -F 'Machine:' <<< "$header" | grep -F "$machine"
  if [ "$libc" = musl ] && readelf --version-info "$path" | grep 'GLIBC_'; then
    echo "glibc dependency in musl payload: $path" >&2; exit 1
  fi
done < <(find "${payloads[0]}" \( -type f -o -type l \) -print0)
if [ "$libc" = musl ]; then
  # The shared-libpython build must still ship Python, just not musl itself.
  find "${payloads[0]}" -name 'libpython*.so*' -print | grep .
fi
