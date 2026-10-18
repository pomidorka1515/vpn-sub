#!/usr/bin/env bash
# Private release libraries; never overwrite the interpreter or system OpenSSL.
set -euo pipefail
prefix="$(realpath -m "$1")"
python="$(realpath "$2")"
version=3.5.9
checksum=603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a
mkdir -p "$prefix/source"
cd "$prefix/source"
curl -fL --retry 3 "https://github.com/openssl/openssl/releases/download/openssl-$version/openssl-$version.tar.gz" -o source.tar.gz
echo "$checksum  source.tar.gz" | sha256sum --check --strict
tar -xf source.tar.gz --strip-components=1
# Keep server/method, PSK and COMP entry points required by prebuilt _ssl.
# Compression backends remain disabled.
# Retain SHA/MD5/BLAKE2, HMAC, PBKDF2 and scrypt for hashlib compatibility.
# Assembly is both faster and often smaller than the generic implementations.
./Configure linux-x86_64 shared --prefix="$prefix" --libdir=lib \
  --openssldir=/etc/ssl -Os -g0 -ffunction-sections -fdata-sections \
  -Wl,--gc-sections -Wl,--as-needed \
  no-ml-kem no-ml-dsa no-slh-dsa no-fips no-legacy no-module \
  no-apps no-tests no-docs no-engine no-dso no-zlib no-zstd no-brotli \
  no-quic no-dtls no-ssl3 no-tls1 no-tls1_1 \
  no-cmp no-cms no-ts no-srp no-srtp \
  no-aria no-bf no-camellia no-cast no-des no-idea \
  no-md2 no-md4 no-mdc2 no-rc2 no-rc4 no-rc5 no-ripemd \
  no-seed no-sm2 no-sm3 no-sm4 no-whirlpool
make -j"${OPENSSL_JOBS:-2}" build_libs
for algorithm in ML_KEM ML_DSA SLH_DSA; do
  grep -Eq "^[[:space:]]*#[[:space:]]*define OPENSSL_NO_$algorithm\b" include/openssl/configuration.h
done
make install_dev
cp LICENSE.txt "$prefix/LICENSE.txt"
strip --strip-unneeded "$prefix/lib/libcrypto.so.3" "$prefix/lib/libssl.so.3"
# Importing the prebuilt extensions is also a strict required-symbol check.
LD_LIBRARY_PATH="$prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" "$python" - <<'PY'
import ssl
import _hashlib
from pathlib import Path

print(ssl.OPENSSL_VERSION)
print(_hashlib.__file__)
if ssl.OPENSSL_VERSION_INFO[:4] != (3, 5, 0, 9):
    raise SystemExit("private OpenSSL was not loaded")
maps = Path('/proc/self/maps').read_text()
print('\n'.join(line for line in maps.splitlines() if 'libcrypto' in line or 'libssl' in line))
PY
