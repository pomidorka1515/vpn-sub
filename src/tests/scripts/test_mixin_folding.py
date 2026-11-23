from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

pytest.importorskip("python_minifier")

from obfuscator.linker import flatten, module_name
from obfuscator.mixins import fold_mixins
from obfuscator.pipeline import obfuscate_modules, prepare_tree
from tests.support import discord as discord_support
from tests.support.config import config_mock, language_config, subscription_config
from tests.support.paths import REPO_ROOT
from tests.support.telegram import subscription_mock, telegram_mock

__all__: list[str] = []


def _link(sources: dict[Path, str]) -> tuple[ast.Module, list[str]]:
    trees = {
        module_name(path): prepare_tree(source, str(path), module=module_name(path))
        for path, source in sources.items()
    }
    return flatten(trees, rename_classes=False), list(trees)


def _database(feature: str, root: str = "class Database(FeatureMixin): pass\n") -> dict[Path, str]:
    return {
        Path("db/features.py"): feature,
        Path("db/database.py"): "from .features import FeatureMixin\n" + root,
    }


def _execute(tree: ast.Module, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    module = ModuleType("_test_folded_mixins")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(tree, "folded.py", "exec"), module.__dict__)  # ruff: ignore[exec-builtin] - Fixed fixtures.
    return module.__dict__


def _fold(sources: dict[Path, str]) -> ast.Module:
    tree, modules = _link(sources)
    fold_mixins(tree, modules)
    return tree


def test_diamond_c3_super_overrides_and_global_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = _database(
        """
value = 'global'
class BaseMixin:
    def run(self, *, suffix='!'):
        return self.label() + suffix
    def label(self): return 'base'
class LeftMixin(BaseMixin):
    def run(self, *, suffix='!'):
        return 'left:' + super().run(suffix=suffix)
class RightMixin(BaseMixin):
    def run(self, *, suffix='!'):
        return 'right:' + super().run(suffix=suffix)
class FeatureMixin(LeftMixin, RightMixin):
    value = 'class'
    def label(self): return value
""",
        """
class Database(FeatureMixin):
    def run(self, *, suffix='!'):
        return 'root:' + super().run(suffix=suffix)
__result__ = Database().run(suffix='?')
""",
    )
    baseline, _ = _link(sources)
    expected = _execute(baseline, monkeypatch)["__result__"]
    tree, modules = _link(sources)
    assert fold_mixins(tree, modules) == 4
    namespace = _execute(tree, monkeypatch)
    assert expected == namespace["__result__"] == "root:left:right:global?"
    assert namespace["Database"].__bases__ == (object,)
    assert [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)] == ["Database"]
    assert not any(isinstance(node, ast.Name) and node.id == "super" for node in ast.walk(tree))


def test_shared_definitions_defaults_data_and_descriptors(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = {
        Path("bots/telegram/common.py"): """
from contextlib import contextmanager, asynccontextmanager
events = []
def default():
    events.append('default')
    return []
class SharedMixin:
    DATA = []
    def append(self, value, items=default()):
        items.append(value)
        return items
    @staticmethod
    def static(value=7): return value
    @classmethod
    def concrete(cls): return cls.__name__
    @contextmanager
    def lease(self): yield self
    @asynccontextmanager
    async def async_lease(self): yield self
events.append('after')
""",
        Path("bots/telegram/admin/bot.py"): """
from ..common import SharedMixin
class TelegramAdminBot(SharedMixin): pass
""",
        Path("bots/telegram/public/bot.py"): """
from ..common import SharedMixin
class TelegramPublicBot(SharedMixin): pass
""",
    }
    tree, modules = _link(sources)
    assert fold_mixins(tree, modules) == 1
    namespace = _execute(tree, monkeypatch)
    admin, public = namespace["TelegramAdminBot"], namespace["TelegramPublicBot"]
    assert namespace["events"] == ["default", "after"]
    assert admin.append is not public.append
    assert admin.append.__defaults__ == public.append.__defaults__
    assert admin.append.__defaults__[0] is public.append.__defaults__[0]
    assert admin.DATA is public.DATA
    assert admin.__dict__["static"] is not public.__dict__["static"]
    assert admin.static() == 7
    assert admin.concrete() == "TelegramAdminBot"
    assert public.concrete() == "TelegramPublicBot"
    assert admin().append(1) == [1]
    assert public().append(2) == [1, 2]
    assert admin.append.__name__ == "append"
    assert admin.append.__qualname__ == "TelegramAdminBot.append"
    assert public.append.__qualname__ == "TelegramPublicBot.append"
    assert admin.lease.__wrapped__.__qualname__ == "TelegramAdminBot.lease"
    instance = admin()
    with instance.lease() as leased:
        assert leased is instance


@pytest.mark.asyncio
async def test_async_super_and_context_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    tree, modules = _link(
        _database("""
from contextlib import asynccontextmanager
class BaseMixin:
    async def close(self): return ['base']
class FeatureMixin(BaseMixin):
    async def close(self): return ['feature', *await super().close()]
    @asynccontextmanager
    async def lease(self): yield self
""")
    )
    fold_mixins(tree, modules)
    instance = _execute(tree, monkeypatch)["Database"]()
    assert await instance.close() == ["feature", "base"]
    async with instance.lease() as leased:
        assert leased is instance


def test_fold_before_allowlist_and_full_minification() -> None:
    result = obfuscate_modules(
        _database(
            """
class FeatureMixin:
    constant = 4
    def handle(self, value): return self.constant + value
""",
            "class Database(FeatureMixin): pass\n__result__ = Database().handle(value=5)\n",
        ),
        [
            "c:Database",
            "f:Database:handle",
            "a:Database:handle:value",
            "v:Database:constant",
        ],
    )
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 9
    assert len(result.renames) == 3
    assert result.renamed_parameters == 2
    assert any(
        isinstance(value, type) and value.__bases__ == (object,)
        for value in namespace.values()
    )


def test_colliding_mixin_names_and_immutable_data_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = {
        Path("db/one.py"): "class FeatureMixin:\n    VERSION = 3\n    def one(self): return 1\n",
        Path("db/two.py"): "class FeatureMixin:\n    def two(self): return 2\n",
        Path("db/database.py"): """
from .one import FeatureMixin as One
from .two import FeatureMixin as Two
class Database(One, Two): pass
__result__ = (Database().one(), Database().two(), One.VERSION)
""",
    }
    tree, modules = _link(sources)
    assert fold_mixins(tree, modules) == 2
    namespace = _execute(tree, monkeypatch)
    assert namespace["__result__"] == (1, 2, 3)
    assert "FeatureMixin" not in namespace
    assert not any(
        name.startswith("_g") and isinstance(value, type) for name, value in namespace.items()
    )


def test_real_method_bodies_and_no_metadata_assignments() -> None:
    tree = _fold(_database("class FeatureMixin:\n    def handle(self): return 1\n"))
    root = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    assert any(isinstance(node, ast.FunctionDef) and node.name == "handle" for node in root.body)
    assert not any(
        isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and node.attr in {"__name__", "__qualname__"}
        for node in ast.walk(tree)
    )


def test_shared_mixin_method_and_keyword_renames_across_consumers() -> None:
    sources = {
        Path("bots/telegram/common.py"): """
class SharedMixin:
    def calculate(self, *, amount): return amount + 1
    def invoke(self, *, value): return self.calculate(amount=value)
""",
        Path("bots/telegram/admin/bot.py"): """
from ..common import SharedMixin
class TelegramAdminBot(SharedMixin): pass
""",
        Path("bots/telegram/public/bot.py"): """
from ..common import SharedMixin
from ..admin.bot import TelegramAdminBot
class TelegramPublicBot(SharedMixin): pass
__result__ = (TelegramAdminBot().invoke(value=3), TelegramPublicBot().calculate(amount=8))
""",
    }
    result = obfuscate_modules(
        sources,
        [
            f"f:{root}:{method}*"
            for root in ("TelegramAdminBot", "TelegramPublicBot")
            for method in ("calculate", "invoke")
        ],
    )
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (4, 9)
    for name in ("TelegramAdminBot", "TelegramPublicBot"):
        cls = namespace[name]
        assert cls.__bases__ == (object,)
        assert "calculate" not in cls.__dict__
        assert "invoke" not in cls.__dict__
        for member, function in cls.__dict__.items():
            if inspect.isfunction(function):
                assert function.__name__ == member
                assert function.__qualname__ == f"{name}.{member}"


def test_parameter_rename_with_lowered_super() -> None:
    result = obfuscate_modules(
        _database(
            """
class BaseMixin:
    def run(self, *, value): return value + 1
class FeatureMixin(BaseMixin):
    def run(self, *, value): return super().run(value=value) * 2
""",
            "class Database(FeatureMixin): pass\n__result__ = Database().run(value=4)\n",
        ),
        ["f:Database:run*"],
    )
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 10


@pytest.mark.parametrize(
    "feature",
    [
        "class FeatureMixin(list): pass",
        "class FeatureMixin[T]: pass",
        "class FeatureMixin(metaclass=type): pass",
        "from typing import final\n@final\nclass FeatureMixin: pass",
        "from typing import Protocol\nclass FeatureMixin(Protocol): pass",
        "class FeatureMixin:\n    __slots__ = ()",
        "class FeatureMixin:\n    def __new__(cls): pass",
        "class FeatureMixin:\n    def __init_subclass__(cls): pass",
        "class FeatureMixin:\n    def __getattribute__(self, name): pass",
        "class FeatureMixin:\n    def method(self): return self.__secret",
        "class FeatureMixin:\n    def method(self): return __class__",
        "class FeatureMixin:\n    @property\n    def method(self): return 1",
        "class FeatureMixin:\n    DATA = 1\n    def method(self, value=DATA): return value",
        "class FeatureMixin:\n    DATA = object()",
        "class FeatureMixin:\n    if True:\n        DATA = 1",
        "class FeatureMixin:\n    def method(self): return 1\n    def method(self): return 2",
        "class FeatureMixin:\n    def method(self): return super(FeatureMixin, self).method()",
        "class FeatureMixin:\n    def method(self): return super().method",
        "class FeatureMixin:\n    def method(self): return super().method()",
        "class FeatureMixin:\n    def method(self):\n        def nested(): return super().method()\n        return nested()",
        "class FeatureMixin:\n    def method(self, super): return super().method()",
        "class FeatureMixin:\n    def method(self): return locals()",
        "class FeatureMixin:\n    def method(self): return self.__dict__",
    ],
)
def test_rejects_unsupported_architecture(feature: str) -> None:
    with pytest.raises(ValueError, match=r"Mixin folding|unresolved internal import"):
        _fold(_database(feature))


@pytest.mark.parametrize(
    "usage",
    [
        "alias = FeatureMixin",
        "instance = FeatureMixin()",
        "check = isinstance(Database(), FeatureMixin)",
        "FeatureMixin.DATA = 2",
        "del FeatureMixin.DATA",
        "value = FeatureMixin.method",
        "value = FeatureMixin.DATA",
        "class Other(FeatureMixin): pass",
        "class Other(Database): pass",
        "Alias = Database\nclass Other(Alias): pass",
        "hierarchy = Database.__bases__",
        "namespace = Database.__dict__",
        "class UnconnectedMixin: pass",
    ],
)
def test_rejects_escaping_mutated_or_uncomposed_mixins(usage: str) -> None:
    sources = _database(
        "class FeatureMixin:\n    DATA = []\n    def method(self): return 1\n",
        "class Database(FeatureMixin): pass\n" + usage + "\n",
    )
    with pytest.raises(ValueError, match="Mixin folding"):
        _fold(sources)


def test_missing_or_rewritten_root_is_an_error() -> None:
    with pytest.raises(ValueError, match="missing composition root"):
        _fold({Path("db/features.py"): "class FeatureMixin: pass"})
    with pytest.raises(ValueError, match="no longer has mixins"):
        _fold(_database("class FeatureMixin: pass", "class Database: pass"))


def test_complete_application_cannot_silently_drop_a_package() -> None:
    with pytest.raises(ValueError, match="missing composition root"):
        _fold({Path(name + ".py"): "" for name in ("main", "app", "asgi", "storage")})


@pytest.mark.parametrize(
    "reflection",
    [
        "import sys\ndef peek(): return sys._getframe(1)",
        "def peek(): return globals()",
        "peek = locals",
    ],
)
def test_rejects_helpers_that_observe_class_definition_frames(reflection: str) -> None:
    with pytest.raises(ValueError, match=r"Mixin folding: (frame|namespace) inspection"):
        _fold(_database(reflection + "\nclass FeatureMixin: pass"))


def test_rejects_inconsistent_c3() -> None:
    sources = _database("""
class XMixin: pass
class YMixin: pass
class AMixin(XMixin, YMixin): pass
class BMixin(YMixin, XMixin): pass
class FeatureMixin(AMixin, BMixin): pass
""")
    with pytest.raises(ValueError, match="inconsistent C3"):
        _fold(sources)


def test_rejects_consumer_dependent_super() -> None:
    sources = {
        Path("bots/telegram/common.py"): """
class SharedMixin:
    def method(self): return super().method()
class OneMixin:
    def method(self): return 1
class TwoMixin:
    def method(self): return 2
""",
        Path("bots/telegram/admin/bot.py"): """
from ..common import SharedMixin, OneMixin
class TelegramAdminBot(SharedMixin, OneMixin): pass
""",
        Path("bots/telegram/public/bot.py"): """
from ..common import SharedMixin, TwoMixin
class TelegramPublicBot(SharedMixin, TwoMixin): pass
""",
    }
    with pytest.raises(ValueError, match="consumer-dependent dispatch"):
        _fold(sources)


def test_rejects_inheritance_between_composition_roots() -> None:
    sources = {
        Path("bots/telegram/admin/bot.py"): """
class FeatureMixin: pass
class TelegramAdminBot(FeatureMixin): pass
""",
        Path("bots/telegram/public/bot.py"): """
from ..admin.bot import TelegramAdminBot
class TelegramPublicBot(TelegramAdminBot): pass
""",
    }
    with pytest.raises(ValueError, match="composition roots cannot inherit"):
        _fold(sources)


def test_rejects_cross_package_bases_even_when_mro_is_cached() -> None:
    sources = {
        Path("bots/discord/common.py"): "class SharedMixin: pass",
        Path("bots/discord/admin/bot.py"): """
from ..common import SharedMixin
class DiscordAdminBot(SharedMixin): pass
""",
        Path("bots/discord/public/bot.py"): """
from ..common import SharedMixin
class DiscordPublicBot(SharedMixin): pass
""",
        Path("bots/telegram/admin/bot.py"): """
from bots.discord.common import SharedMixin
class TelegramAdminBot(SharedMixin): pass
""",
        Path("bots/telegram/public/bot.py"): """
class FeatureMixin: pass
class TelegramPublicBot(FeatureMixin): pass
""",
    }
    with pytest.raises(ValueError, match="base outside its closed package"):
        _fold(sources)


def test_folding_is_deterministic() -> None:
    sources = _database("class BaseMixin: pass\nclass FeatureMixin(BaseMixin):\n    DATA = 1")
    results: list[str] = []
    for _ in range(2):
        tree, modules = _link(sources)
        fold_mixins(tree, modules)
        results.append(ast.unparse(tree))
    assert results[0] == results[1]


@pytest.fixture(scope="module")
def real_sources() -> dict[Path, str]:
    return {
        path.relative_to(REPO_ROOT / "src"): path.read_text(encoding="utf-8")
        for package in ("db", "bots/telegram", "bots/discord")
        for path in sorted((REPO_ROOT / "src" / package).rglob("*.py"))
    }


def test_real_families_preserve_effective_members_and_signatures(
    real_sources: dict[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree, modules = _link(real_sources)
    baseline = _execute(tree, monkeypatch)
    roots = (
        "Database",
        "TelegramAdminBot",
        "TelegramPublicBot",
        "DiscordAdminBot",
        "DiscordPublicBot",
    )
    expected: dict[str, dict[str, object]] = {}
    for name in roots:
        cls = baseline[name]
        expected[name] = {
            member: inspect.getattr_static(cls, member)
            for base in cls.__mro__[:-1]
            for member in base.__dict__
            if not (member.startswith("__") and member != "__init__")
        }
    assert fold_mixins(tree, modules) > 40
    folded = _execute(tree, monkeypatch)
    for name in roots:
        cls = folded[name]
        assert cls.__bases__ == (object,)
        for member, original in expected[name].items():
            actual = inspect.getattr_static(cls, member)
            if isinstance(original, staticmethod):
                original, actual = original.__func__, cast(staticmethod[Any, Any], actual).__func__
            if inspect.isfunction(original):
                assert inspect.isfunction(actual)
                # Separately executed builds have distinct sentinel instances.
                assert str(inspect.signature(actual)) == str(inspect.signature(original)), (
                    name,
                    member,
                )
                assert actual.__name__ == original.__name__
                assert actual.__qualname__ == f"{name}.{member}"
            else:
                assert actual == original, (name, member)
    assert not any(
        isinstance(value, type) and value.__name__.endswith("Mixin") for value in folded.values()
    )


@pytest.mark.asyncio
async def test_real_folded_discord_commands_and_routing(
    real_sources: dict[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _execute(_fold(real_sources), monkeypatch)
    for name in ("DiscordPublicBot", "DiscordAdminBot", "SharedDiscordClient"):
        monkeypatch.setattr(discord_support, name, namespace[name])
    public, admin, _, sub = discord_support.make_bots()
    try:
        public_call = discord_support.FakeInteraction(guild_id=123)
        await public.cmd_info(cast(Any, public_call))
        sub.business_svc.get_info.assert_awaited_once_with("alice")
        assert public_call.response.deferred
        assert "alice" in str(public_call.response.messages[-1]["content"])
        component = discord_support.FakeInteraction(data={"custom_id": "menu_info"})
        await public.dispatch_component(cast(Any, component))
        assert sub.business_svc.get_info.await_count == 2
        admin_call = discord_support.FakeInteraction()
        await admin.cmd_admin(cast(Any, admin_call))
        assert admin_call.response.messages
        for bot in (public, admin):
            assert bot.__class__.__bases__ == (object,)
        assert all(callable(getattr(admin, handler)) for _, handler, _ in admin.ROUTES)
    finally:
        await public.rendering.close()
        await public.bot.close()


@pytest.mark.asyncio
async def test_real_folded_telegram_construction_routing_and_super_shutdown(
    real_sources: dict[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _execute(_fold(real_sources), monkeypatch)
    telegram = telegram_mock()
    constructor = MagicMock(return_value=telegram)
    monkeypatch.setitem(namespace, "AsyncTeleBot", constructor)
    config = config_mock(
        subscription_config(
            bot={"token": "1:token", "whitelist": [42]},
            publicbot={"token": "2:token"},
        )
    )
    language = config_mock(language_config())
    sub = subscription_mock()
    admin = namespace["TelegramAdminBot"](sub=sub, cfg=config, lang_cfg=language)
    public = namespace["TelegramPublicBot"](
        sub=sub,
        cfg=config,
        lang_cfg=language,
        limiter=MagicMock(),
    )
    assert constructor.call_count == 2
    for bot in (admin, public):
        assert bot.__class__.__bases__ == (object,)
        assert bot._polling_task is None
        assert all(callable(getattr(bot, handler)) for _, handler, _ in bot.ROUTES)
        await bot.close()
        await bot.close()
        assert bot._stopping.is_set()
        assert bot._handlers.active_count == 0
        assert bot._close_task.done()


def test_cli_rejects_rewritten_architecture_without_publishing_output(tmp_path: Path) -> None:
    source, output = tmp_path / "source", tmp_path / "output"
    sources = _database("class FeatureMixin:\n    __slots__ = ()\n")
    for path, contents in sources.items():
        destination = source / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(contents, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "obfuscator", str(source), str(output), "--allowlist", "[]"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Mixin folding" in result.stderr
    assert not (output / "app.min.py").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("backup", [False, True])
async def test_real_folded_database_start_transaction_backup_and_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    backup: bool,
) -> None:
    if backup:
        request.getfixturevalue("hardlinks")
    sources = {
        path.relative_to(REPO_ROOT / "src"): path.read_text(encoding="utf-8")
        for path in sorted((REPO_ROOT / "src/db").rglob("*.py"))
    }
    tree, modules = _link(sources)
    fold_mixins(tree, modules)
    namespace = _execute(tree, monkeypatch)
    database = namespace["Database"]
    db = database(
        path=tmp_path / "state.sqlite3",
        backup_dir=tmp_path / "backups" if backup else None,
        start_backup=False,
    )
    await db.start()
    try:
        await db.create_user(
            username="folded",
            uuid="uuid",
            token="token",
            fingerprint="chrome",
            displayname="Folded",
        )
        assert await db.token_to_user("token") == "folded"
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE users SET displayname = ? WHERE username = ?", ("Updated", "folded")
            )
        if backup:
            await db.backup_now()
        async with db.connection() as conn:
            assert (await conn.rows("PRAGMA integrity_check"))[0][0] == "ok"
    finally:
        await db.close()
    reopened = database(path=tmp_path / "state.sqlite3", start_backup=False)
    await reopened.start()
    try:
        assert await reopened.token_to_user("token") == "folded"
    finally:
        await reopened.close()


def test_unrelated_packages_are_untouched() -> None:
    tree, modules = _link({Path("worker.py"): "class Mixin: pass\nclass Worker(Mixin): pass"})
    before = ast.dump(tree)
    assert fold_mixins(tree, modules) == 0
    assert ast.dump(tree) == before
