from __future__ import annotations

import socket
from types import SimpleNamespace

import psutil
import pytest

from util import SysUtil


def test_ipaddr_uses_interface_addresses_not_hosts_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_addrs() -> dict[str, list[SimpleNamespace]]:
        return {
            "lo": [
                SimpleNamespace(family=socket.AF_INET, address="127.0.0.1"),
                SimpleNamespace(family=socket.AF_INET6, address="::1"),
            ],
            "ens3": [
                SimpleNamespace(family=socket.AF_INET, address="203.0.113.10"),
                SimpleNamespace(family=socket.AF_INET6, address="2001:db8::10"),
                SimpleNamespace(
                    family=socket.AF_INET6,
                    address="fe80::5054:ff:fe44:b5d9%ens3",
                ),
            ],
            "warp": [
                SimpleNamespace(family=socket.AF_INET, address="172.16.0.2"),
            ],
        }

    monkeypatch.setattr(psutil, "net_if_addrs", fake_addrs)
    ips = SysUtil.ipaddr()
    assert ips.ipv4 == ("203.0.113.10", "172.16.0.2")
    assert ips.ipv6 == ("2001:db8::10",)


def test_ipaddr_returns_none_when_only_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        psutil,
        "net_if_addrs",
        lambda: {
            "lo": [SimpleNamespace(family=socket.AF_INET, address="127.0.0.1")],
        },
    )
    ips = SysUtil.ipaddr()
    assert ips.ipv4 is None
    assert ips.ipv6 is None


def test_app_memory_returns_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Mem:
        rss = 20 * 1024 * 1024
        swap = 4096

    class _Proc:
        def memory_full_info(self) -> _Mem:
            return _Mem()

        def children(self, recursive: bool = False) -> list[_Proc]:
            return []

    monkeypatch.setattr(psutil, "Process", lambda: _Proc())
    memory = SysUtil.app_memory()
    assert memory.ram == 20 * 1024 * 1024
    assert memory.swap == 4096


def test_app_memory_skips_dead_children(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Mem:
        rss = 1024
        swap = 0

    class _Dead:
        def memory_full_info(self) -> _Mem:
            raise psutil.NoSuchProcess(1)

    class _Proc:
        def memory_full_info(self) -> _Mem:
            return _Mem()

        def children(self, recursive: bool = False) -> list[_Dead]:
            return [_Dead()]

    monkeypatch.setattr(psutil, "Process", lambda: _Proc())
    assert SysUtil.app_memory().ram == 1024
