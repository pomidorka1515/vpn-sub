from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest

pytest.importorskip("python_minifier")

from obfuscator.annotations import TypeHint
from obfuscator.linker import flatten, module_name
from obfuscator.mixins import fold_mixins
from obfuscator.pipeline import obfuscate_modules, prepare_tree
from tests.support import api as api_support
from tests.support import discord as discord_support
from tests.support.config import config_mock, language_config, subscription_config
from tests.support.paths import REPO_ROOT
from tests.support.telegram import subscription_mock, telegram_mock

if TYPE_CHECKING:
    from config import Config, LangConfig
    from db import Database

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
        isinstance(value, type) and value.__bases__ == (object,) for value in namespace.values()
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
__classes__ = TelegramAdminBot, TelegramPublicBot
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


def test_folded_inherited_field_hints_keep_downstream_receiver_renames_safe() -> None:
    result = obfuscate_modules(
        _database(
            """
class Value:
    def read(self, *, increment): return 7 + increment
class BaseMixin:
    value: object
class FeatureMixin(BaseMixin):
    value: Value
    def read(self): return read_feature(self)
def read_feature(feature: FeatureMixin):
    return feature.value.read(increment=1)
""",
            """
from .features import Value
class Database(FeatureMixin): pass
__classes__ = Database, Value
""",
        ),
        ["f:Value:read*"],
    )
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    database, value = namespace["__classes__"]
    instance = database()
    instance.value = value()
    assert instance.read() == 8
    assert "read" not in value.__dict__


def test_folding_keeps_all_inherited_field_type_alternatives() -> None:
    tree = _fold(
        _database("""
class First: pass
class Second: pass
class BaseMixin:
    value: First
class FeatureMixin(BaseMixin):
    value: Second
""")
    )
    root = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Database"
    )
    hint = root.__dict__["field_types"]["value"]
    assert isinstance(hint, TypeHint)
    assert hint.classes == ("db.features.Second", "db.features.First")


def test_shared_mixin_annotation_hints_keep_all_consumers_and_container_structure() -> None:
    tree = _fold(
        {
            Path("bots/telegram/common.py"): """
class SharedMixin: pass
def first(values: list[SharedMixin]) -> SharedMixin: return values[0]
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
    )
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "first"
    )
    result = function.__dict__["return_type"]
    parameter = function.args.args[0].__dict__["type_annotation"]
    assert isinstance(result, TypeHint)
    assert isinstance(parameter, TypeHint)
    assert result.classes == (
        "bots.telegram.admin.bot.TelegramAdminBot",
        "bots.telegram.public.bot.TelegramPublicBot",
    )
    assert parameter.origin == "sequence"
    assert parameter.arguments == (result,)


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


@pytest.mark.parametrize("family", ["db", "api"])
def test_cli_rejects_rewritten_architecture_without_publishing_output(
    tmp_path: Path,
    api_sources: dict[Path, str],
    family: str,
) -> None:
    source, output = tmp_path / "source", tmp_path / "output"
    sources = _database("class FeatureMixin:\n    __slots__ = ()\n")
    if family == "api":
        sources = dict(api_sources)
        path = Path("api/web/session.py")
        sources[path] = sources[path].replace("@requires_webapi_auth", "@unknown")
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


@pytest.fixture(scope="module")
def api_sources() -> dict[Path, str]:
    return {
        path.relative_to(REPO_ROOT / "src"): path.read_text(encoding="utf-8")
        for path in sorted((REPO_ROOT / "src/api").rglob("*.py"))
    }


@pytest.mark.parametrize("package", ["admin", "web"])
def test_real_api_folding_preserves_members_routes_and_shared_base(
    api_sources: dict[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    package: str,
) -> None:
    sources = {
        path: source
        for path, source in api_sources.items()
        if path.parts[1] not in {"admin", "web"} or path.parts[1] == package
    }
    tree, modules = _link(sources)
    baseline = _execute(tree, monkeypatch)
    name = "AsyncApi" if package == "admin" else "AsyncWebApi"
    original = baseline[name]
    expected = {
        member: inspect.getattr_static(original, member)
        for base in original.__mro__
        if base.__name__ not in {"AsyncBaseApi", "ABC", "object"}
        for member in base.__dict__
        if (not member.startswith("__") or member == "__init__") and member != "_abc_impl"
    }
    assert fold_mixins(tree, modules) > 1
    folded = _execute(tree, monkeypatch)
    actual = folded[name]
    assert actual.__bases__ == (folded["AsyncBaseApi"],)
    assert actual.ROUTES == original.ROUTES
    for member, value in expected.items():
        result = inspect.getattr_static(actual, member)
        if isinstance(value, staticmethod):
            value, result = value.__func__, result.__func__
        if isinstance(value, property):
            value, result = value.fget, result.fget
        if inspect.isfunction(value):
            assert str(inspect.signature(value)) == str(inspect.signature(result)), member
            assert result.__qualname__ == f"{name}.{member}"
        else:
            assert result == value, member
    assert not any(
        isinstance(value, type)
        and value.__module__ == folded["__name__"]
        and value.__name__.endswith(("Mixin", "Routes"))
        for value in folded.values()
    )


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ('Route("GET", "invalid", "gui_auth")', "Route path must start"),
        ('Route("GET", "/invalid", "missing_handler")', "no such method exists"),
        ('Route("GET", "/invalid", "gui_auth", rate_limit=0)', "rate_limit must be positive"),
    ],
)
def test_folded_api_keeps_import_time_route_validation(
    api_sources: dict[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
    message: str,
) -> None:
    sources = dict(api_sources)
    path = Path("api/web/static.py")
    sources[path] = sources[path].replace('Route("GET", "/auth", "gui_auth")', replacement)
    with pytest.raises((ValueError, TypeError), match=message):
        _execute(_fold(sources), monkeypatch)


@pytest.mark.parametrize(
    ("path", "old", "new", "message"),
    [
        ("api/web/routes.py", "    AsyncBaseApi,", "", "directly retain AsyncBaseApi"),
        ("api/web/routes.py", "    AsyncAccountRoutes,", "", "unconnected mixin"),
        (
            "api/web/_base.py",
            "class AsyncWebApiMixin:",
            "class AsyncWebApiMixin(list):",
            "unknown or external base",
        ),
        (
            "api/web/session.py",
            "    @requires_webapi_auth",
            "    @unknown",
            "unsupported method decorator",
        ),
        (
            "api/admin/ui.py",
            "    @property",
            "    @unknown\n    @property",
            "unsupported method decorator",
        ),
        (
            "api/web/static.py",
            'Route("GET", "/auth", "gui_auth")',
            'Route("GET", dynamic(), "gui_auth")',
            "unsupported executable class body",
        ),
        (
            "api/web/static.py",
            "route.handler for route in AsyncAccountRoutes.ROUTES",
            "route.handler for route in unknown()",
            "unsupported executable class body",
        ),
        ("api/web/static.py", '"gui_panel",', "dynamic(),", "unsupported executable class body"),
        (
            "api/common.py",
            "class AsyncBaseApi(ABC):",
            "class AsyncBaseApi(list):",
            "unsupported shared API base",
        ),
        (
            "api/web/_base.py",
            "class AsyncWebApiMixin:",
            "from api.common import AsyncBaseApi\nclass AsyncWebApiMixin(AsyncBaseApi):",
            "only API composition roots",
        ),
        (
            "api/web/routes.py",
            "class AsyncWebApi(",
            "class ExtraRoutes: pass\nclass AsyncWebApi(",
            "unconnected mixin",
        ),
    ],
)
def test_api_folding_rejects_unknown_architecture(
    api_sources: dict[Path, str],
    path: str,
    old: str,
    new: str,
    message: str,
) -> None:
    sources = dict(api_sources)
    assert old in sources[Path(path)]
    sources[Path(path)] = sources[Path(path)].replace(old, new)
    with pytest.raises(ValueError, match=message):
        _fold(sources)


@pytest.mark.parametrize(
    "usage",
    [
        "instance = AsyncStaticRoutes()",
        "alias = AsyncStaticRoutes",
        "AsyncStaticRoutes.ROUTES = ()",
        "del AsyncStaticRoutes.ROUTES",
        "class Other(AsyncStaticRoutes): pass",
        "class Other(AsyncWebApi): pass",
        "class Other(AsyncBaseApi):\n    ROUTES = ()",
    ],
)
def test_api_folding_rejects_runtime_group_uses_and_extra_consumers(
    api_sources: dict[Path, str],
    usage: str,
) -> None:
    sources = dict(api_sources)
    sources[Path("api/web/routes.py")] += "\n" + usage + "\n"
    with pytest.raises(ValueError, match="Mixin folding"):
        _fold(sources)


@pytest.mark.asyncio
@pytest.mark.parametrize("minified", [False, True])
async def test_real_folded_apis_http_auth_validation_permissions_and_cache_headers(
    api_sources: dict[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    language: Config[LangConfig],
    minified: bool,
) -> None:
    sources = dict(api_sources)
    sources[Path("main.py")] = (
        "from api.application import create_api_application\n__factory__ = create_api_application\n"
    )
    tree = (
        ast.parse(obfuscate_modules(sources, ["f:AsyncApi:_auth_update_root:patch"]).source)
        if minified
        else _fold(sources)
    )
    namespace = _execute(tree, monkeypatch)
    monkeypatch.setattr(api_support, "create_api_application", namespace["create_api_application"])
    async with api_support.api_app() as app:
        sub = await api_support.make_subscription(database, app=app, lang_cfg=language)
        cfg = api_support.config_snapshot(
            subscription_config(
                api_uri="api",
                api_token="secret",
                api_admin_ui_auth=["admin", "panel-secret"],
            )
        )
        kwargs = {
            "app": app,
            "cfg": cfg,
            "sub": sub,
            "bw": api_support.make_watch(database, sub),
            "limiter": app.extensions["limiter"],
        }
        admin = namespace["AsyncApi"](audit_cfg=api_support.Audit(), **kwargs)
        public = namespace["AsyncWebApi"](**kwargs)
        assert app.extensions["frontend_web_api"] is public
        for api in (admin, public):
            assert all(callable(getattr(api, route.handler)) for route in api.ROUTES)
        client = app.test_client()
        root = {"Authorization": "secret"}
        prefix = "/sub/api/api"
        assert (await client.get(prefix + "/user/list")).status_code == 401
        assert (await client.get(prefix + "/user/list", headers=root)).status_code == 200
        invalid = await client.post(prefix + "/user/add", headers=root, json={"user": 1})
        assert invalid.status_code == 400
        assert invalid.headers["Cache-Control"] == "no-store"
        created = await client.post(
            prefix + "/auth/create",
            headers=root,
            json={
                "kind": "tokens",
                "name": "reader",
                "permissions": ["user.read"],
            },
        )
        assert created.status_code == 201
        headers = {"Authorization": "Bearer " + (await created.get_json())["obj"]["token"]}
        assert (await client.get(prefix + "/user/list", headers=headers)).status_code == 200
        assert (await client.get(prefix + "/logs/audit", headers=headers)).status_code == 403
        login = await client.post(
            "/sub/admin/session", json={"username": "admin", "password": "panel-secret"}
        )
        assert login.status_code == 200
        token = await client.get("/sub/admin/token")
        assert token.status_code == 200
        assert (await token.get_json())["obj"]["permissions"] == ["*"]
        denied = await client.get("/sub/webapi/stats")
        assert denied.status_code == 401
        assert denied.headers["Cache-Control"] == "no-store"
        bad_login = await client.post("/sub/webapi/login", json={"username": 1, "password": "x"})
        assert bad_login.status_code == 400
        assert bad_login.headers["Cache-Control"] == "no-store"
        page = await client.get("/sub/auth")
        assert page.status_code == 200
        assert page.headers["Cache-Control"] == "no-store"
        missing = await client.get(
            "/sub/reset?token=invalid", headers={"User-Agent": "Firefox/120.0"}
        )
        assert missing.status_code == 404
        assert missing.mimetype == "text/html"
        assert missing.headers["Cache-Control"] == "no-store"
        await database.create_user(
            username="alice", uuid="uuid", token="a" * 40, fingerprint="chrome", displayname="Alice"
        )
        await database.set_auth_token("alice", "a" * 100)
        client.set_cookie("localhost", "auth_token", "a" * 100)
        stats = await client.get("/sub/webapi/stats")
        assert stats.status_code == 200
        assert stats.headers["Cache-Control"] == "no-store"
        register = await client.post("/sub/webapi/register", json={})
        assert register.status_code == 403
        await admin.live_state.close()
