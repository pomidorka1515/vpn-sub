from __future__ import annotations

import socket
from pathlib import Path
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


def test_cpu_since_last_does_not_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    samples = iter(((100, 80), (200, 150), (300, 200)))
    monkeypatch.setattr(SysUtil, "_cpu_sample", staticmethod(lambda: next(samples)))
    monkeypatch.setattr(SysUtil, "_cpu_prev", None)

    def fail_sleep(_seconds: float) -> None:
        raise AssertionError("polling cpu sample must not sleep")

    monkeypatch.setattr("util.time.sleep", fail_sleep)
    assert SysUtil.cpu_since_last() == 0.0
    assert SysUtil.cpu_since_last() == 30.0
    assert SysUtil.cpu_since_last() == 50.0


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


def test_app_threads_uses_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Switches:
        voluntary = 4000
        involuntary = 21

    class _Native:
        def __init__(self, tid: int) -> None:
            self.tid = tid

        def name(self) -> str:
            return "MainThread" if self.tid == 1024 else "Thread-1"

        def status(self) -> str:
            return "sleeping" if self.tid == 1024 else "waiting"

        def num_ctx_switches(self) -> _Switches:
            return _Switches()

    class _Current:
        pid = 7

        def num_threads(self) -> int:
            return 2

        def threads(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(id=1024, user_time=10.2, system_time=2.2),
                SimpleNamespace(id=1025, user_time=0.1, system_time=0.1),
            ]

    def process(pid: int | None = None) -> _Current | _Native:
        if pid is None:
            return _Current()
        return _Native(pid)

    monkeypatch.setattr(psutil, "Process", process)
    monkeypatch.setattr(psutil, "PROCFS_PATH", "/missing-proc")

    threads = SysUtil.app_threads()
    assert SysUtil.app_thread_amount() == 2
    assert [(item.tid, item.name, item.state, item.cpu, item.ctx_switches) for item in threads] == [
        (1024, "MainThread", "sleeping", 12.4, 4021),
        (1025, "Thread-1", "waiting", 0.2, 4021),
    ]
    assert all(item.stack is None for item in threads)


def test_app_threads_reads_stack_and_skips_dead(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    task = tmp_path / "9" / "task" / "1024"
    task.mkdir(parents=True)
    (task / "status").write_text("Name:\tMainThread\nVmStk:\t64 kB\n", encoding="utf-8")

    class _Switches:
        voluntary = 1
        involuntary = 0

    class _Live:
        def name(self) -> str:
            return "MainThread"

        def status(self) -> str:
            return "running"

        def num_ctx_switches(self) -> _Switches:
            return _Switches()

    class _Dead:
        def num_ctx_switches(self) -> _Switches:
            raise psutil.NoSuchProcess(1026)

    class _Current:
        pid = 9

        def threads(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(id=1024, user_time=1.0, system_time=0.0),
                SimpleNamespace(id=1026, user_time=0.0, system_time=0.0),
            ]

    def process(pid: int | None = None) -> _Current | _Live | _Dead:
        if pid is None:
            return _Current()
        return _Live() if pid == 1024 else _Dead()

    monkeypatch.setattr(psutil, "Process", process)
    monkeypatch.setattr(psutil, "PROCFS_PATH", str(tmp_path))

    threads = SysUtil.app_threads()
    assert len(threads) == 1
    assert threads[0].stack == 64 * 1024
    assert threads[0].state == "running"
