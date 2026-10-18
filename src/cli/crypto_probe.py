"""Release acceptance checks, executed by the actual packaged interpreter."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import ssl
from pathlib import Path


def check_hashes() -> None:
    # Empty-message vectors also catch missing provider algorithms with fallback
    # extensions excluded. BLAKE2 uses its dedicated CPython module instead.
    vectors = {
        "md5": "d41d8cd98f00b204e9800998ecf8427e",
        "sha1": "da39a3ee5e6b4b0d3255bfef95601890afd80709",
        "sha224": "d14a028c2a3a2bc9476102bb288234c415a2b01f828ea62ac5b3e42f",
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "sha384": "38b060a751ac96384cd9327eb1b1e36a21fdb71114be07434c0cc7bf63f6e1da274edebfe76f65fbd51ad2f14898b95b",
        "sha512": "cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e",
        "sha3_256": "a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a",
        "blake2b": "786a02f742015903c6c6fd852552d272912f4740e15847618a86e217f71f5419d25e1031afee585313896444934eb04b903a685b1448b755d56f701afe9be2ce",
        "blake2s": "69217a3079908094e11121d042354a7c1f55b6482ca1a51e1b250dfd1ed0eef9",
    }
    for name, expected in vectors.items():
        if getattr(hashlib, name)(b"").hexdigest() != expected:
            raise RuntimeError(f"digest failed: {name}")
    for name in ("sha3_224", "sha3_384", "sha3_512"):
        if not getattr(hashlib, name)(b"abc").digest():
            raise RuntimeError(f"digest unavailable: {name}")
    for name in ("shake_128", "shake_256"):
        if len(getattr(hashlib, name)(b"abc").digest(32)) != 32:
            raise RuntimeError(f"XOF failed: {name}")
    if hmac.digest(b"key", b"The quick brown fox jumps over the lazy dog", "sha256").hex() != "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8":
        raise RuntimeError("HMAC failed")
    if hashlib.pbkdf2_hmac("sha256", b"password", b"salt", 1).hex() != "120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b":
        raise RuntimeError("PBKDF2 failed")
    if hashlib.scrypt(b"", salt=b"", n=16, r=1, p=1, dklen=64).hex() != "77d6576238657b203b19ca42c18a0497f16b4844e3074ae8dfdffa3fede21442fcd0069ded0948f8326a753a0fc81f17e8d3e0fb2e0d3628cf35e20c38d18906":
        raise RuntimeError("scrypt failed")


async def check_tls(cafile: str, port: int, version: str) -> None:
    import aiohttp
    import requests

    context = ssl.create_default_context(cafile=cafile)
    context.set_alpn_protocols(["http/1.1"])
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", port, ssl=context, server_hostname="localhost",
    )
    try:
        connection = writer.get_extra_info("ssl_object")
        if connection.version() != version or connection.selected_alpn_protocol() != "http/1.1":
            raise RuntimeError("TLS version/ALPN mismatch")
        writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        await writer.drain()
        if b"200 OK" not in await reader.read():
            raise RuntimeError("TLS HTTP request failed")
    finally:
        writer.close()
        await writer.wait_closed()
    for rejected_context, hostname in (
        (ssl.create_default_context(), "localhost"),
        (context, "wrong.invalid"),
    ):
        try:
            _, unexpected = await asyncio.open_connection(
                "127.0.0.1", port, ssl=rejected_context, server_hostname=hostname,
            )
        except ssl.SSLCertVerificationError:
            pass
        else:
            unexpected.close()
            await unexpected.wait_closed()
            raise RuntimeError("invalid certificate accepted")
    async with aiohttp.ClientSession() as client:
        async with client.get(f"https://localhost:{port}/", ssl=context) as response:
            if response.status != 200:
                raise RuntimeError("aiohttp HTTPS failed")
        async with client.ws_connect(f"https://localhost:{port}/ws", ssl=context) as websocket:
            await websocket.send_str("probe")
            if (await websocket.receive()).data != "probe":
                raise RuntimeError("WebSocket TLS failed")
    with requests.Session() as client:
        client.trust_env = False
        result = client.get(f"https://localhost:{port}/", verify=cafile, timeout=10)
        if result.status_code != 200:
            raise RuntimeError("Requests HTTPS failed")


def probe(manifest: Path) -> None:
    from paths import bundled_root

    check_hashes()
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if document["private"]:
        if ssl.OPENSSL_VERSION_INFO[:4] != (3, 5, 0, 9):
            raise RuntimeError(f"unexpected OpenSSL: {ssl.OPENSSL_VERSION}")
        # Check the mapped files, not merely the build-time linker metadata.
        payload = bundled_root().resolve()
        for line in Path("/proc/self/maps").read_text(encoding="utf-8").splitlines():
            if "libssl.so" in line or "libcrypto.so" in line:
                if not Path(line.split()[-1]).is_relative_to(payload):
                    raise RuntimeError(f"OpenSSL loaded outside payload: {line}")
    for endpoint in document["endpoints"]:
        asyncio.run(check_tls(endpoint["ca"], endpoint["port"], endpoint["version"]))
    print(f"crypto probe passed: {ssl.OPENSSL_VERSION}, hashes, verified TLS and WSS")
