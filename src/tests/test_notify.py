from __future__ import annotations

import time
from typing import Any, cast

from custom_types import UserRecord
from db import Database
from notify import Kind, Notifier, account_key, classify, restored, split_key
from notify.events import Notice


TEXTS = {
    "en": {
        "warning_expiry_soon": "{days} days",
        "warning_expired": "expired",
        "warning_traffic_soon": "{used_gb}/{limit_gb} {percent}",
        "warning_traffic_exhausted": "off {limit_gb}",
        "warning_whitelist_soon": "wl {used_gb}/{limit_gb} {percent}",
        "warning_whitelist_exhausted": "wl off {limit_gb}",
        "warning_restored": "back",
        "warning_whitelist_restored": "wl back",
    },
    "ru": {"warning_expired": "истекла"},
}


def _state(**overrides: Any) -> UserRecord:
    row: dict[str, Any] = {
        "username": "alice",
        "uuid": "u",
        "token": "t",
        "auth_token": None,
        "fingerprint": "chrome",
        "displayname": "Alice",
        "enabled": 1,
        "enabled_time": 1,
        "enabled_wl": 1,
        "expires_at": 0,
        "bw_limit_gb": 0,
        "bw_used": 0,
        "wl_limit_gb": 0,
        "wl_used": 0,
        "ext_username": None,
        "ext_password_hash": None,
        "created_at": 0,
    }
    row.update(overrides)
    return cast(UserRecord, row)


def _kinds(state: UserRecord, now: int = 1_000_000) -> tuple[Kind, ...]:
    return tuple(notice.kind for notice in classify(state, now))


def test_unlimited_never_warns() -> None:
    assert classify(_state(), 1_000_000) == ()


def test_expiry_buckets_are_separate() -> None:
    now = 1_000_000
    three = classify(_state(expires_at=now + 3 * 86400 + 10), now)
    two = classify(_state(expires_at=now + 2 * 86400 + 10), now)
    assert three[0].kind is Kind.EXPIRY_SOON
    assert three[0].bucket == "3"
    assert two[0].bucket == "2"
    assert three[0].marker != two[0].marker


def test_expiry_does_not_warn_four_days_out() -> None:
    now = 1_000_000
    assert classify(_state(expires_at=now + 4 * 86400), now) == ()


def test_expired_only_while_still_enabled() -> None:
    now = 1_000_000
    assert _kinds(_state(expires_at=now - 1)) == (Kind.EXPIRED,)
    assert classify(_state(expires_at=now - 1, enabled_time=0), now) == ()


def test_traffic_80_then_95_then_exhausted() -> None:
    limit = 10
    now = 1_000_000
    soon = classify(_state(bw_limit_gb=limit, bw_used=int(limit * 10**9 * 0.81)), now)
    later = classify(_state(bw_limit_gb=limit, bw_used=int(limit * 10**9 * 0.96)), now)
    done = classify(_state(bw_limit_gb=limit, bw_used=limit * 10**9 + 1), now)
    assert soon[0].bucket == "80"
    assert later[0].bucket == "95"
    assert done[0].kind is Kind.TRAFFIC_EXHAUSTED
    assert "traffic_soon" in done[0].clears
    # 9.6 GB must not render as 10/10
    assert soon[0].fields["used_gb"] == 8
    assert later[0].fields["used_gb"] == 9


def test_whitelist_is_independent_of_main() -> None:
    state = _state(
        bw_limit_gb=10, bw_used=1,
        wl_limit_gb=2, wl_used=2 * 10**9 + 1,
    )
    assert _kinds(state) == (Kind.WHITELIST_EXHAUSTED,)


def test_disabled_main_does_not_repeat_near_limit() -> None:
    state = _state(enabled=0, bw_limit_gb=10, bw_used=int(9.6 * 10**9))
    assert classify(state, 1_000_000) == ()


class _Sink:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[int, str]] = []

    def __call__(self, tgid: int, text: str) -> None:
        if self.fail:
            raise RuntimeError("blocked")
        self.sent.append((tgid, text))


def _notifier(sink: _Sink, lang: str = "en") -> Notifier:
    return Notifier(texts=TEXTS, send=sink, language=lambda _tgid: lang)


def test_send_failure_does_not_mark(database: Database) -> None:
    sink = _Sink(fail=True)
    notifier = _notifier(sink)
    seen: set[str] = set()
    now = int(time.time())
    notice = classify(_state(expires_at=now - 1), now)[0]
    assert notifier.notify("alice", 7, notice, seen) is False
    assert seen == set()
    assert not database.notification_seen(account_key("alice", notice.marker), "")


def test_second_call_is_a_no_op() -> None:
    sink = _Sink()
    notifier = _notifier(sink)
    notice = Notice(Kind.EXPIRED, "1", {})
    seen = {account_key("alice", notice.marker)}
    assert notifier.notify("alice", 7, notice, seen) is False
    assert sink.sent == []


def test_missing_key_and_bad_format_are_not_sent() -> None:
    sink = _Sink()
    missing = Notice(Kind.RESTORED, "1", {}, clears=("expired",))
    seen = {"alice:expired:1"}
    empty = Notifier(texts={"en": {}}, send=sink, language=lambda _tgid: "en")
    assert empty.notify("alice", 7, missing, seen) is False
    broken = Notifier(
        texts={"en": {"warning_restored": "back {missing} extra"}},
        send=sink,
        language=lambda _tgid: "en",
    )
    again = {"alice:expired:1"}
    assert broken.notify("alice", 7, missing, again) is False
    assert sink.sent == []
    assert seen == {"alice:expired:1"} and again == {"alice:expired:1"}


def test_recovery_requires_a_prior_episode() -> None:
    sink = _Sink()
    notifier = _notifier(sink)
    notice = restored(main=True)[0]
    seen: set[str] = set()
    assert notifier.notify("alice", 7, notice, seen) is False
    seen.add(account_key("alice", "traffic_exhausted:1"))
    assert notifier.notify("alice", 7, notice, seen) is True
    assert sink.sent == [(7, "back")]
    assert account_key("alice", "traffic_exhausted:1") not in seen
    assert account_key("alice", notice.marker) in seen


def test_second_outage_can_announce_recovery_again() -> None:
    sink = _Sink()
    notifier = _notifier(sink)
    outage = Notice(Kind.TRAFFIC_EXHAUSTED, "1", {"limit_gb": 1}, clears=("traffic_soon",))
    recovery = restored(main=True)[0]
    seen: set[str] = set()

    assert notifier.notify("alice", 7, outage, seen) is True
    assert notifier.notify("alice", 7, recovery, seen) is True
    assert notifier.notify("alice", 7, outage, seen) is True
    assert notifier.notify("alice", 7, recovery, seen) is True
    assert [text for _tgid, text in sink.sent] == ["off 1", "back", "off 1", "back"]


def test_prefix_username_does_not_inherit_markers() -> None:
    sink = _Sink()
    notifier = _notifier(sink)
    notice = Notice(Kind.EXPIRED, "1", {})
    seen = {account_key("alice", notice.marker)}
    assert split_key(account_key("alice", notice.marker)) == ("alice", notice.marker)
    assert split_key("legacy") is None
    assert notifier.notify("alic", 7, notice, seen) is True
    assert notifier.notify("alice", 7, notice, seen) is False
    assert sink.sent == [(7, "expired")]


def test_kind_clear_does_not_match_a_longer_kind() -> None:
    sink = _Sink()
    notifier = _notifier(sink)
    notice = Notice(Kind.TRAFFIC_EXHAUSTED, "1", {"limit_gb": 1}, clears=("traffic",))
    seen = {account_key("alice", "traffic_soon:80"), account_key("alice", "traffic:1")}
    assert notifier.notify("alice", 7, notice, seen) is True
    assert account_key("alice", "traffic_soon:80") in seen
    assert account_key("alice", "traffic:1") not in seen


def test_accounts_do_not_share_a_marker() -> None:
    sink = _Sink()
    notifier = _notifier(sink)
    notice = Notice(Kind.EXPIRED, "1", {})
    alice: set[str] = set()
    bob: set[str] = set()
    assert notifier.notify("alice", 7, notice, alice) is True
    assert notifier.notify("bob", 7, notice, bob) is True
    assert len(sink.sent) == 2
