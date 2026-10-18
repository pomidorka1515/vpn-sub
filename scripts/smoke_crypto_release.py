"""Test packaged crypto against independent loopback TLS servers."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import ssl
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp import WSMsgType, web

ROOT = Path(__file__).resolve().parents[1]


async def http(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def websocket(request: web.Request) -> web.WebSocketResponse:
    response = web.WebSocketResponse()
    await response.prepare(request)
    async for message in response:
        if message.type == WSMsgType.TEXT:
            await response.send_str(message.data)
    return response


def certificate(directory: Path, algorithm: str) -> Path:
    cert = directory / f"{algorithm}.pem"
    key = directory / f"{algorithm}.key"
    command = ["openssl", "req", "-x509", "-newkey"]
    command += ["rsa:2048"] if algorithm == "rsa" else ["ec", "-pkeyopt", "ec_paramgen_curve:P-256"]
    subprocess.run(
        [*command, "-nodes", "-sha256", "-days", "1", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost", "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
        # Independent system executable/libraries, not the candidate build.
        env={key: value for key, value in os.environ.items() if key != "LD_LIBRARY_PATH"},
    )
    return cert


def audit(prefix: Path, binary: Path) -> None:
    import _ssl  # ruff: ignore[import-private-name] - audit the extension's ELF dependencies
    from compression import zstd

    baseline = subprocess.check_output(
        ["/usr/bin/ldd", _ssl.__file__], text=True,
        env={key: value for key, value in os.environ.items() if key != "LD_LIBRARY_PATH"},
    )
    payloads = list(binary.parent.glob("*.dist"))
    if len(payloads) != 1:
        raise RuntimeError("expected one Nuitka payload")
    payload = payloads[0]
    for name in ("libcrypto.so.3", "libssl.so.3"):
        candidates = list(payload.rglob(name))
        if len(candidates) != 1:
            raise RuntimeError(f"missing or duplicated {name}")
        expected = hashlib.sha256((prefix / "lib" / name).read_bytes()).digest()
        # Nuitka patches RPATH, so compare the code section rather than the ELF.
        with TemporaryDirectory() as temporary:
            sections: list[bytes] = []
            for index, library in enumerate((prefix / "lib" / name, candidates[0])):
                section = Path(temporary) / str(index)
                subprocess.run(
                    ["/usr/bin/objcopy", "--dump-section", f".text={section}",
                     str(library), str(Path(temporary) / f"copy-{index}")], check=True,
                )
                sections.append(section.read_bytes())
            if sections[0] != sections[1]:
                raise RuntimeError(f"payload did not use private {name}: {expected.hex()}")
        print(f"private payload {name}: {candidates[0].stat().st_size:,} bytes")
        original = next(Path(line.split("=>", 1)[1].split()[0]) for line in baseline.splitlines() if name in line)
        old, new = original.read_bytes(), candidates[0].read_bytes()
        print(
            f"{name}: raw {len(old):,} -> {len(new):,}; "
            f"zstd-19 {len(zstd.compress(old, level=19)):,} -> {len(zstd.compress(new, level=19)):,} bytes",
        )
    for library in payload.rglob("*.so*"):
        if not library.is_file() or library.read_bytes()[:4] != b"\x7fELF":
            continue
        dynamic = subprocess.check_output(["/usr/bin/readelf", "-d", str(library)], text=True)
        if str(prefix) in dynamic:
            raise RuntimeError(f"private build path leaked into {library}")
        if library.name.startswith(("libssl", "libcrypto")) and library.name not in {"libssl.so.3", "libcrypto.so.3"}:
            raise RuntimeError(f"extra OpenSSL library: {library}")


async def smoke(directory: Path, binary: Path | None, prefix: Path | None) -> None:
    runners: list[web.AppRunner] = []
    endpoints: list[dict[str, str | int]] = []
    try:
        for algorithm in ("rsa", "ec"):
            cert = certificate(directory, algorithm)
            for version in (ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3):
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = context.maximum_version = version
                context.load_cert_chain(cert, directory / f"{algorithm}.key")
                context.set_alpn_protocols(["http/1.1"])
                app = web.Application()
                app.router.add_get("/", http)
                app.router.add_get("/ws", websocket)
                runner = web.AppRunner(app)
                runners.append(runner)
                await runner.setup()
                site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
                await site.start()
                port = runner.addresses[0][1]
                endpoints.append({"ca": str(cert), "port": port, "version": "TLSv1.2" if version == ssl.TLSVersion.TLSv1_2 else "TLSv1.3"})
        manifest = directory / "crypto.json"
        manifest.write_text(json.dumps({"private": binary is not None, "endpoints": endpoints}))
        command = [str(binary)] if binary else [sys.executable, "-m", "main"]
        env = dict(os.environ)
        # A standalone binary must find its own libraries without assistance.
        env.pop("LD_LIBRARY_PATH", None)
        if binary is None and prefix is not None:
            env["LD_LIBRARY_PATH"] = str(prefix / "lib")
        env["PYTHONPATH"] = str(ROOT / "src")
        process = await asyncio.create_subprocess_exec(
            *command, "--crypto-probe", str(manifest), env=env, cwd=directory,
        )
        try:
            async with asyncio.timeout(90):
                if await process.wait() != 0:
                    raise RuntimeError("crypto probe failed")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
    finally:
        for runner in runners:
            await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--openssl-prefix", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve() if args.binary else None
    if binary:
        if not args.openssl_prefix:
            parser.error("--binary requires --openssl-prefix")
        audit(args.openssl_prefix.resolve(), binary)
    with TemporaryDirectory(prefix="vpn-sub-crypto-") as temporary:
        asyncio.run(smoke(Path(temporary), binary, args.openssl_prefix))


if __name__ == "__main__":
    main()
