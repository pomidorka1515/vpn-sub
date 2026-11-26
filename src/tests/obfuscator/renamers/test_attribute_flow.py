from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from obfuscator.pipeline import obfuscate_modules
from obfuscator.renamers.allowlist import AllowlistRenamer

if TYPE_CHECKING:
    from collections.abc import Callable

__all__: list[str] = []

RECORD: str = """
class Record:
    def __init__(self): self._value = 7
record = Record()
"""


def execute(source: str) -> dict[str, Any]:
    tree = ast.parse(source)
    AllowlistRenamer(tree).apply(["attr:Record:_value"])
    namespace: dict[str, Any] = {}
    exec(ast.unparse(tree), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    return namespace


@pytest.mark.parametrize("usage", [
    "def invoke(callback, tx): return callback(tx)\n__result__ = invoke(lambda item: item._value, record)",
    ("def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "alias = read\n__result__ = invoke(tx=record, callback=alias)"),
    ("def read(item): return getattr(item, '_value')\n"
     "def invoke(callback=read, *, tx): return callback(tx)\n__result__ = invoke(tx=record)"),
    ("def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "__result__ = invoke(**{'callback': read, 'tx': record})"),
    ("def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "def forward(action, item): return invoke(action, item)\n__result__ = forward(read, record)"),
    ("def read(item): return item._value\ndef other(item): return getattr(item, '_value')\n"
    "def invoke(callback, tx): return callback(tx)\n"
    "__result__ = invoke(read if True else other, record)"),
    ("def identity(item): return item\ndef forward(item): return identity(item)\n"
     "__result__ = forward(record)._value"),
    ("def read(item, count):\n if count: return read(item, count - 1)\n return item._value\n"
     "__result__ = read(record, 3)"),
    ("def factory(): return record\ndef invoke(callback): return callback()\n"
     "__result__ = invoke(factory)._value"),
    ("class Reader:\n def read(self, item): return item._value\n"
    "def invoke(callback, tx): return callback(tx)\nreader = Reader()\n"
    "alias = reader.read\n__result__ = invoke(alias, record)"),
    ("class Holder:\n def __init__(self, item): self.item = item\n"
     "holder = Holder(record)\n__result__ = holder.item._value"),
    ("class Holder:\n def __init__(self, item): self.item = item\n"
     "holder = Holder(record)\n__result__ = getattr(holder, 'item')._value"),
    ("class Holder: pass\nholder = Holder()\ndef register(callback): holder.callback = callback\n"
     "def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "register(read)\n__result__ = invoke(holder.callback, record)"),
])
def test_closed_receiver_and_callback_flows(usage: str) -> None:
    assert execute(RECORD + usage)["__result__"] == 7


@pytest.mark.parametrize("wrap", [False, True])
@pytest.mark.parametrize("loop", [False, True])
@pytest.mark.parametrize("linked", [False, True])
def test_conditional_callback_wrapper_and_descriptor_binding(wrap: bool, loop: bool, linked: bool) -> None:
    source = f"""
from __future__ import annotations
from collections.abc import Callable
from functools import wraps
from typing import Concatenate
__all__: list[str] = []
class Record:
    def __init__(self) -> None: self._value: int = 7
    def read(self) -> int: return self._value
record: Record = Record()
type Method[**P, R] = Callable[Concatenate[Record, P], R]
def decorate[**P, R](count: int) -> Callable[[Method[P, R]], Method[P, R]]:
    def decorator(function: Method[P, R]) -> Method[P, R]:
        @wraps(function)
        def wrapper(self: Record, *args: P.args, **kwargs: P.kwargs) -> R:
            return function(self, *args, **kwargs)
        return wrapper
    return decorator
def register(record: Record) -> int:
    {'for name in ("read",):' if loop else 'if True:'}
        try:
            function: Method[[], int] = getattr(type(record), 'read')
        except AttributeError:
            return 0
        if {wrap}:
            function = decorate(10)(function)
        function = function.__get__(record, type(record))
        return function()
    return 0
__result__: int = register(record)
"""
    if linked:
        result = obfuscate_modules({Path("main.py"): source}, ["attr:Record:_value"], jobs=1)
        namespace: dict[str, Any] = {}
        exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    else:
        namespace = execute(source)
    assert namespace["__result__"] == 7


def test_conditional_callback_retains_opaque_alternative() -> None:
    source = RECORD + """
from functools import wraps
def decorate(function):
    @wraps(function)
    def wrapper(self: Record, *args, **kwargs):
        return function(self, *args, **kwargs)
    return wrapper
def register(record: Record):
    function = external
    if flag:
        function = decorate(function)
    function = function.__get__(record, type(record))
    return function()
register(record)
"""
    with pytest.raises(ValueError, match=r"Escaping|Unresolved|Ambiguous"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


def test_exception_handler_callback_alternative_is_not_discarded() -> None:
    source = RECORD + """
def read(item: Record) -> int: return item._value
def invoke(item: Record) -> int:
    try:
        callback = read
    except Exception:
        callback = external
    return callback(item)
invoke(record)
"""
    with pytest.raises(ValueError, match="unresolved consumer"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


def test_wraps_does_not_hide_an_escape_in_the_wrapper_body() -> None:
    source = RECORD + """
from functools import wraps
def read(item: Record) -> int: return item._value
@wraps(read)
def wrapper(item: Record) -> int:
    external(item)
    return read(item)
wrapper(record)
"""
    with pytest.raises(ValueError, match="unresolved consumer"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


@pytest.mark.parametrize("view", ["keys", "values"])
@pytest.mark.parametrize("comprehension", [False, True])
def test_mapping_views_track_selected_receivers(view: str, comprehension: bool) -> None:
    key, value = ("record", "1") if view == "keys" else ("'key'", "record")
    iterable = f"[element for element in cache.{view}()]" if comprehension else f"cache.{view}()"
    source = RECORD + f"""
cache = {{}}
cache[{key}] = {value}
for item in {iterable}:
    __result__ = item._value
"""
    assert execute(source)["__result__"] == 7


@pytest.mark.parametrize("view", ["keys", "values"])
@pytest.mark.parametrize("comprehension", [False, True])
def test_mapping_views_do_not_hide_escaping_receivers(view: str, comprehension: bool) -> None:
    key, value = ("record", "1") if view == "keys" else ("'key'", "record")
    iterable = f"[element for element in cache.{view}()]" if comprehension else f"cache.{view}()"
    source = RECORD + f"""
cache = {{}}
cache[{key}] = {value}
external({iterable})
"""
    with pytest.raises(ValueError, match="Escaping"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


@pytest.mark.parametrize("usage", [
    "def invoke(callback, tx): return callback(tx)\ninvoke(external, record)",
    ("def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "invoke(read if flag else external, record)"),
    "def helper(item): external(item)\nhelper(record)",
    "def helper(item): return getattr(item, input())\nhelper(record)",
    "def helper(item): return item.__dict__\nhelper(record)",
    "def identity(item): return item\nexternal(identity(record))",
    "def read(item): return item._value\nread(record)\nexternal(read)",
    ("def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "external(invoke)\ninvoke(read, record)"),
    "items = [record]\nexternal(items)",
    "unknown.field = record",
    ("class Holder:\n def __init__(self, item): self.item = item\n"
     "holder = Holder(record)\nexternal(holder)"),
    ("class Holder:\n def __init__(self, item): self.item = item\n"
     "holder = Holder(record)\nexternal(getattr(holder, 'item'))"),
    ("class Holder:\n def __init__(self, item): self.item = item\n"
     "holder = Holder(record)\nexternal(vars(holder))"),
    ("def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "unknown.callback = invoke\ninvoke(read, record)"),
    "external(record.__init__)",
    ("def decorate(function): return external(function)\nclass Reader:\n"
     " @decorate\n def read(self, item): return item._value\nReader().read(record)"),
    ("class Reader:\n def read(self, item): return item._value\nclass Unknown: pass\n"
     "reader = Reader() if flag else Unknown()\nreader.read(record)"),
    ("class Holder: pass\nholder = Holder()\ndef read(item): return item._value\n"
     "holder.callback = read\nsetattr(holder, 'callback', external)\nholder.callback(record)"),
    "record.unknown_method()",
    "def invoke(callback, tx): return callback(tx)\ninvoke(*arguments, tx=record)",
    ("def read(item): return item._value\ndef invoke(callback, tx): return callback(tx)\n"
     "invoke(read, record, **arguments)"),
    "def invoke(callback, tx): return callback(tx)\ninvoke(record=record, callback=external)",
])
def test_unverifiable_flows_fail_closed(usage: str) -> None:
    with pytest.raises(ValueError, match=r"Escaping|reflect|Reflect|Unresolved|Ambiguous"):
        AllowlistRenamer(ast.parse(RECORD + usage)).apply(["attr:Record:_value"])


@pytest.mark.parametrize("reaches_record", [False, True])
def test_escape_check_handles_shared_paths_and_cycles(reaches_record: bool) -> None:
    # Repeated paths to the same class must not multiply the work, and a cycle
    # must not hide a receiver that is reachable through another field.
    depth = 24
    declarations = [f"class Layer{i}: pass\nlayer{i} = Layer{i}()" for i in range(depth)]
    fields = [
        f"layer{i}.left = layer{i + 1}\nlayer{i}.right = layer{i + 1}"
        for i in range(depth - 1)
    ]
    source = RECORD + "\n".join([*declarations, *fields, f"layer{depth - 1}.back = layer0"])
    if reaches_record:
        source += f"\nlayer{depth - 1}.record = record"
    tree = ast.parse(source + "\nexternal(layer0)\n")
    if reaches_record:
        with pytest.raises(ValueError, match="unresolved consumer"):
            AllowlistRenamer(tree).apply(["attr:Record:_value"])
    else:
        assert "attr:Record:_value" in AllowlistRenamer(tree).apply(["attr:Record:_value"])


def test_context_transaction_callbacks_after_linking_and_automatic_renames() -> None:
    source = """
from typing import Self
class ConfigTransaction:
    def __init__(self): self.data = 7
    def __enter__(self) -> Self: return self
    def __exit__(self, *args): pass
class Config:
    def edit(self) -> ConfigTransaction: return ConfigTransaction()
    def mutate(self, callback):
        with self.edit() as tx:
            return callback(tx)
def read(item): return item.data
__result__ = Config().mutate(read)
"""
    result = obfuscate_modules({Path("main.py"): source}, ["attr:ConfigTransaction:data"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7


def test_bound_static_and_class_callbacks() -> None:
    source = RECORD + """
class Reader:
    @staticmethod
    def static(item): return item._value
    @classmethod
    def class_method(cls, item): return item._value
def invoke(callback, tx): return callback(tx)
__result__ = invoke(Reader.static, record), invoke(Reader.class_method, record)
"""
    assert execute(source)["__result__"] == (7, 7)


@pytest.mark.parametrize("class_method", [False, True])
@pytest.mark.parametrize("positional_only", [False, True])
def test_shared_forwarder_preserves_bound_callback_receivers(class_method: bool, positional_only: bool) -> None:
    decorator = "@classmethod\n    " if class_method else ""
    receiver = "cls" if class_method else "self"
    separator = ", /" if positional_only else ""
    source = f"""
class Record:
    _value = 7
    {decorator}def save({receiver}{separator}): return {receiver}.write(None)
    {decorator}def write({receiver}, value): return {receiver}._value
record = Record()
def forward(function, *args, **kwargs): return function(*args, **kwargs)
def identity(item): return item
__result__ = forward(record.save), forward(identity, 1)
"""
    assert execute(source)["__result__"] == (7, 1)


def test_shared_forwarder_still_checks_unbound_callback_receivers() -> None:
    source = RECORD + """
def forward(function, *args, **kwargs): return function(*args, **kwargs)
forward(Record.__init__, unknown)
"""
    with pytest.raises(ValueError, match=r"Unresolved|Ambiguous|Unverifiable"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


def test_unknown_callback_does_not_partially_mutate_tree() -> None:
    tree = ast.parse(RECORD + "def invoke(callback, tx): callback(tx)\ninvoke(external, record)")
    before = ast.dump(tree)
    with pytest.raises(ValueError, match="unresolved consumer"):
        AllowlistRenamer(tree).apply(["attr:Record:_value"])
    assert ast.dump(tree) == before


def test_function_descriptor_binding_through_record_member_names() -> None:
    source = RECORD + """
from typing import NamedTuple
class Dispatch(NamedTuple):
    name: str
    def invoke(self, owner):
        function = getattr(type(owner), self.name)
        bound = function.__get__(owner, type(owner))
        return bound()
class Service:
    def __init__(self, item): self.item = item
    def read(self): return self.item._value
service = Service(record)
__result__ = Dispatch('read').invoke(service)
"""
    assert execute(source)["__result__"] == 7


def test_binding_does_not_execute_function_body() -> None:
    source = RECORD + """
class Service:
    def __init__(self, item): self.item = item
def read(service):
    raise RuntimeError('must not be called')
service = Service(record)
bound = read.__get__(service, type(service))
__result__ = bound.__self__ is service
"""
    assert execute(source)["__result__"] is True


@pytest.mark.parametrize("function", ["unknown", "read if flag else unknown"])
def test_opaque_descriptor_binding_still_rejects_affected_receivers(function: str) -> None:
    source = RECORD + f"""
class Service:
    def __init__(self, item): self.item = item
def read(service): return service.item._value
service = Service(record)
function = {function}
function.__get__(service, type(service))
"""
    with pytest.raises(ValueError, match="unresolved consumer"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


def test_external_bound_callback_does_not_expose_unrelated_receiver_fields() -> None:
    source = RECORD + """
class Service:
    def __init__(self, item): self.item = item
    def read(self): return self.item._value
service = Service(record)
callbacks = []
callbacks.append(service.read)
__result__ = callbacks[0]()
"""
    assert execute(source)["__result__"] == 7


@pytest.mark.parametrize("usage", [
    "__returned__ = external(lambda: record)",
    "def factory(): return record\n__returned__ = external(factory)",
    """class Service:
    def __init__(self, item): self.item = item
    def read(self): return self.item
service = Service(record)
__returned__ = external(service.read)""",
])
def test_external_callbacks_can_return_selected_receivers(usage: str) -> None:
    def invoke(callback: Callable[[], object]) -> object:
        return callback()

    tree = ast.parse(RECORD + usage + "\n__result__ = record._value")
    AllowlistRenamer(tree).apply(["attr:Record:_value"])
    namespace: dict[str, Any] = {"external": invoke}
    exec(ast.unparse(tree), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__returned__"] is namespace["record"]
    assert namespace["__result__"] == 7


def test_thread_dispatch_can_return_selected_receivers() -> None:
    source = RECORD + """
import asyncio
async def dispatch(function, *args, **kwargs):
    def worker(): return function(*args, **kwargs)
    return await asyncio.to_thread(worker)
def identity(item): return item
__result__ = asyncio.run(dispatch(identity, record)) is record, record._value
"""
    assert execute(source)["__result__"] == (True, 7)


def test_external_bound_callback_on_selected_receiver_is_rejected() -> None:
    source = RECORD + "\nexternal(record.__init__)"
    with pytest.raises(ValueError, match="through bound callback"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


@pytest.mark.parametrize("method", ["before_serving", "after_serving"])
def test_quart_lifecycle_registration_accepts_bound_callback(method: str) -> None:
    source = f"""
import asyncio
from quart import Quart
class Record:
    def __init__(self): self._value = 7
    async def callback(self): self._value += 1
record = Record()
app = Quart('quart')
app.{method}(record.callback)
asyncio.run(app.{method}_funcs[0]())
__result__ = record._value
"""
    assert execute(source)["__result__"] == 8


@pytest.mark.parametrize("method", [
    "before_request", "after_request", "teardown_request",
    "before_websocket", "after_websocket", "teardown_websocket",
])
def test_quart_request_hooks_accept_bound_callback(method: str) -> None:
    source = f"""
import asyncio
from quart import Quart
class Record:
    def __init__(self): self._value = 7
    async def callback(self, response): return self._value + response
record = Record()
app = Quart('quart')
app.{method}(record.callback)
__result__ = asyncio.run(app.{method}_funcs[None][0](2))
"""
    assert execute(source)["__result__"] == 9


def test_unrelated_lifecycle_registration_does_not_accept_bound_callback() -> None:
    source = RECORD + "\nexternal.after_serving(record.__init__)"
    with pytest.raises(ValueError, match="through bound callback"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


def test_quart_lifecycle_registration_on_annotated_parameter() -> None:
    source = """
import asyncio
from quart import Quart as Application
class Record:
    def __init__(self): self._value = 7
    async def callback(self): self._value += 1
def register(app: Application, record: Record):
    app.after_serving(record.callback)
record = Record()
app = Application('quart')
register(app, record)
asyncio.run(app.after_serving_funcs[0]())
__result__ = record._value
"""
    assert execute(source)["__result__"] == 8


@pytest.mark.parametrize("argument", ["'/test', 'test', record.callback", "'/test', endpoint='test', view_func=record.callback"])
def test_quart_route_registration_accepts_bound_callback(argument: str) -> None:
    source = f"""
import asyncio
from quart import Quart
class Record:
    def __init__(self): self._value = 7
    async def callback(self): return self._value
record = Record()
app = Quart('quart')
app.add_url_rule({argument})
__result__ = asyncio.run(app.view_functions['test']())
"""
    assert execute(source)["__result__"] == 7


def test_quart_route_registration_does_not_trust_other_arguments() -> None:
    source = RECORD + "\nfrom quart import Quart\napp = Quart('quart')\napp.add_url_rule(record.__init__, 'test', None)"
    with pytest.raises(ValueError, match="through bound callback"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


def test_quart_route_registration_through_annotated_instance_field() -> None:
    source = """
import asyncio
from quart import Quart
class Base:
    def __init__(self, app: Quart): self.app = app
class Record(Base):
    def __init__(self, app):
        super().__init__(app)
        self._value = 7
    async def callback(self): return self._value
def register(record: Record):
    record.app.add_url_rule('/test', 'test', record.callback)
app = Quart('quart')
record = Record(app)
register(record)
__result__ = asyncio.run(app.view_functions['test']())
"""
    result = obfuscate_modules({Path("main.py"): source}, ["attr:Record:_value"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 7


def test_quart_request_hook_through_inherited_instance_field() -> None:
    source = """
import asyncio
from quart import Quart
class Base:
    def __init__(self, app: Quart): self.app = app
class Record(Base):
    def __init__(self, app):
        super().__init__(app)
        self._value = 7
        self.app.after_request(self.callback)
    async def callback(self, response): return self._value + response
app = Quart('quart')
record = Record(app)
__result__ = asyncio.run(app.after_request_funcs[None][0](2))
"""
    result = obfuscate_modules({Path("main.py"): source}, ["attr:Record:_value"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 9


def test_quart_positional_hook_allows_bound_method_parameter_renames() -> None:
    source = """
import asyncio
from quart import Quart
class Record:
    async def callback(self, response): return response + 1
record = Record()
app = Quart('quart')
app.after_request(record.callback)
__result__ = asyncio.run(app.after_request_funcs[None][0](2))
"""
    tree = ast.parse(source)
    AllowlistRenamer(tree).apply(["f:Record:callback*"])
    namespace: dict[str, Any] = {}
    exec(ast.unparse(tree), namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == 3


def test_quart_renamed_hook_runs_on_real_request() -> None:
    source = """
import asyncio
from quart import Quart
class Record:
    async def callback(self, response):
        response.headers['X-Test'] = 'ok'
        return response
async def view(): return 'body'
app = Quart('quart')
app.add_url_rule('/', 'view', view)
record = Record()
app.after_request(record.callback)
async def request():
    response = await app.test_client().get('/')
    return response.status_code, response.headers['X-Test']
__result__ = asyncio.run(request())
"""
    result = obfuscate_modules({Path("main.py"): source}, ["f:Record:callback*"], jobs=1)
    namespace: dict[str, Any] = {}
    exec(result.source, namespace)  # ruff: ignore[exec-builtin] - Fixed fixture.
    assert namespace["__result__"] == (200, "ok")


@pytest.mark.parametrize("registration", [
    "app.add_url_rule('/test', 'test', record.callback)",
    "unknown.after_request(record.callback)",
])
def test_unknown_or_keyword_dispatch_rejects_method_parameter_renames(registration: str) -> None:
    source = f"""
from quart import Quart
class Record:
    async def callback(self, response): return response
record = Record()
app = Quart('quart')
{registration}
"""
    with pytest.raises(ValueError, match="Escaping callable with allowlisted parameters"):
        AllowlistRenamer(ast.parse(source)).apply(["f:Record:callback*"])


def test_quart_lifecycle_registration_checks_callback_body() -> None:
    source = """
from quart import Quart
class Record:
    def __init__(self): self._value = 7
    def callback(self): unknown(self)
record = Record()
app = Quart('quart')
app.after_serving(record.callback)
"""
    with pytest.raises(ValueError, match="unresolved consumer"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


def test_external_callback_body_still_checks_direct_receiver_escapes() -> None:
    source = RECORD + """
class Service:
    def __init__(self, item): self.item = item
    def read(self):
        unknown(self.item)
        return self.item
service = Service(record)
external(service.read)
"""
    with pytest.raises(ValueError, match="unresolved consumer"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


def test_custom_descriptor_is_not_treated_as_function_binding() -> None:
    source = RECORD + """
class Descriptor:
    def __get__(self, instance, owner): return unknown(instance)
def decorate(function): return Descriptor()
class Service:
    def __init__(self, item): self.item = item
    @decorate
    def read(self): return self.item._value
service = Service(record)
function = getattr(type(service), 'read')
function.__get__(service, type(service))
"""
    with pytest.raises(ValueError, match="unresolved consumer"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


@pytest.mark.parametrize("usage", [
    "items = []\nitems.append(record)\n__result__ = items[0]._value",
    "items = list([record])\nfor item in reversed(items): __result__ = item._value",
    "for item, flag in zip([record], [False], strict=True): __result__ = item._value",
    "for index, item in enumerate([record]): __result__ = item._value",
    "item, flag = (record, False)\n__result__ = item._value",
    ("def read(item, **fields): return item._value\n"
     "def forward(item, **fields): return read(item, **fields)\n"
     "__result__ = forward(record, label='test')"),
])
def test_closed_container_and_keyword_forwarding(usage: str) -> None:
    assert execute(RECORD + usage)["__result__"] == 7


def test_container_escape_does_not_hide_unknown_callback_arguments() -> None:
    source = RECORD + """
def read(item): return item._value
read(record)
unknown([read])
"""
    with pytest.raises(ValueError, match=r"Ambiguous|Unresolved"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])


def test_loop_rebindings_preserve_polymorphic_alternatives() -> None:
    source = RECORD + """
class Other:
    _value = 1
def run():
    item = Other()
    for flag in [True]:
        if flag:
            item = record
        print(item._value)
run()
"""
    with pytest.raises(ValueError, match="Ambiguous"):
        AllowlistRenamer(ast.parse(source)).apply(["attr:Record:_value"])
