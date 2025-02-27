from __future__ import annotations

import contextlib
import inspect
import io
import os
import re
import sys
import tempfile
import textwrap
import unittest
from typing import Any, Callable, Iterator

import mypy.stubtest
from mypy.stubtest import parse_options, test_stubs
from mypy.test.data import root_dir


@contextlib.contextmanager
def use_tmp_dir(mod_name: str) -> Iterator[str]:
    current = os.getcwd()
    current_syspath = sys.path[:]
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.chdir(tmp)
            if sys.path[0] != tmp:
                sys.path.insert(0, tmp)
            yield tmp
        finally:
            sys.path = current_syspath[:]
            if mod_name in sys.modules:
                del sys.modules[mod_name]

            os.chdir(current)


TEST_MODULE_NAME = "test_module"


stubtest_typing_stub = """
Any = object()

class _SpecialForm:
    def __getitem__(self, typeargs: Any) -> object: ...

Callable: _SpecialForm = ...
Generic: _SpecialForm = ...
Protocol: _SpecialForm = ...
Union: _SpecialForm = ...

class TypeVar:
    def __init__(self, name, covariant: bool = ..., contravariant: bool = ...) -> None: ...

class ParamSpec:
    def __init__(self, name: str) -> None: ...

AnyStr = TypeVar("AnyStr", str, bytes)
_T = TypeVar("_T")
_T_co = TypeVar("_T_co", covariant=True)
_K = TypeVar("_K")
_V = TypeVar("_V")
_S = TypeVar("_S", contravariant=True)
_R = TypeVar("_R", covariant=True)

class Coroutine(Generic[_T_co, _S, _R]): ...
class Iterable(Generic[_T_co]): ...
class Mapping(Generic[_K, _V]): ...
class Match(Generic[AnyStr]): ...
class Sequence(Iterable[_T_co]): ...
class Tuple(Sequence[_T_co]): ...
def overload(func: _T) -> _T: ...
"""

stubtest_builtins_stub = """
from typing import Generic, Mapping, Sequence, TypeVar, overload

T = TypeVar('T')
T_co = TypeVar('T_co', covariant=True)
KT = TypeVar('KT')
VT = TypeVar('VT')

class object:
    __module__: str
    def __init__(self) -> None: pass
class type: ...

class tuple(Sequence[T_co], Generic[T_co]): ...
class dict(Mapping[KT, VT]): ...

class function: pass
class ellipsis: pass

class int: ...
class float: ...
class bool(int): ...
class str: ...
class bytes: ...

class list(Sequence[T]): ...

def property(f: T) -> T: ...
def classmethod(f: T) -> T: ...
def staticmethod(f: T) -> T: ...
"""


def run_stubtest(
    stub: str, runtime: str, options: list[str], config_file: str | None = None
) -> str:
    with use_tmp_dir(TEST_MODULE_NAME) as tmp_dir:
        with open("builtins.pyi", "w") as f:
            f.write(stubtest_builtins_stub)
        with open("typing.pyi", "w") as f:
            f.write(stubtest_typing_stub)
        with open(f"{TEST_MODULE_NAME}.pyi", "w") as f:
            f.write(stub)
        with open(f"{TEST_MODULE_NAME}.py", "w") as f:
            f.write(runtime)
        if config_file:
            with open(f"{TEST_MODULE_NAME}_config.ini", "w") as f:
                f.write(config_file)
            options = options + ["--mypy-config-file", f"{TEST_MODULE_NAME}_config.ini"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            test_stubs(parse_options([TEST_MODULE_NAME] + options), use_builtins_fixtures=True)
        # remove cwd as it's not available from outside
        return (
            output.getvalue()
            .replace(os.path.realpath(tmp_dir) + os.sep, "")
            .replace(tmp_dir + os.sep, "")
        )


class Case:
    def __init__(self, stub: str, runtime: str, error: str | None):
        self.stub = stub
        self.runtime = runtime
        self.error = error


def collect_cases(fn: Callable[..., Iterator[Case]]) -> Callable[..., None]:
    """run_stubtest used to be slow, so we used this decorator to combine cases.

    If you're reading this and bored, feel free to refactor this and make it more like
    other mypy tests.

    """

    def test(*args: Any, **kwargs: Any) -> None:
        cases = list(fn(*args, **kwargs))
        expected_errors = set()
        for c in cases:
            if c.error is None:
                continue
            expected_error = c.error
            if expected_error == "":
                expected_error = TEST_MODULE_NAME
            elif not expected_error.startswith(f"{TEST_MODULE_NAME}."):
                expected_error = f"{TEST_MODULE_NAME}.{expected_error}"
            assert expected_error not in expected_errors, (
                "collect_cases merges cases into a single stubtest invocation; we already "
                "expect an error for {}".format(expected_error)
            )
            expected_errors.add(expected_error)
        output = run_stubtest(
            stub="\n\n".join(textwrap.dedent(c.stub.lstrip("\n")) for c in cases),
            runtime="\n\n".join(textwrap.dedent(c.runtime.lstrip("\n")) for c in cases),
            options=["--generate-allowlist"],
        )

        actual_errors = set(output.splitlines())
        assert actual_errors == expected_errors, output

    return test


class StubtestUnit(unittest.TestCase):
    @collect_cases
    def test_basic_good(self) -> Iterator[Case]:
        yield Case(
            stub="def f(number: int, text: str) -> None: ...",
            runtime="def f(number, text): pass",
            error=None,
        )
        yield Case(
            stub="""
            class X:
                def f(self, number: int, text: str) -> None: ...
            """,
            runtime="""
            class X:
                def f(self, number, text): pass
            """,
            error=None,
        )

    @collect_cases
    def test_types(self) -> Iterator[Case]:
        yield Case(
            stub="def mistyped_class() -> None: ...",
            runtime="class mistyped_class: pass",
            error="mistyped_class",
        )
        yield Case(
            stub="class mistyped_fn: ...", runtime="def mistyped_fn(): pass", error="mistyped_fn"
        )
        yield Case(
            stub="""
            class X:
                def mistyped_var(self) -> int: ...
            """,
            runtime="""
            class X:
                mistyped_var = 1
            """,
            error="X.mistyped_var",
        )

    @collect_cases
    def test_coroutines(self) -> Iterator[Case]:
        yield Case(stub="def bar() -> int: ...", runtime="async def bar(): return 5", error="bar")
        # Don't error for this one -- we get false positives otherwise
        yield Case(stub="async def foo() -> int: ...", runtime="def foo(): return 5", error=None)
        yield Case(stub="def baz() -> int: ...", runtime="def baz(): return 5", error=None)
        yield Case(
            stub="async def bingo() -> int: ...", runtime="async def bingo(): return 5", error=None
        )

    @collect_cases
    def test_arg_name(self) -> Iterator[Case]:
        yield Case(
            stub="def bad(number: int, text: str) -> None: ...",
            runtime="def bad(num, text) -> None: pass",
            error="bad",
        )
        if sys.version_info >= (3, 8):
            yield Case(
                stub="def good_posonly(__number: int, text: str) -> None: ...",
                runtime="def good_posonly(num, /, text): pass",
                error=None,
            )
            yield Case(
                stub="def bad_posonly(__number: int, text: str) -> None: ...",
                runtime="def bad_posonly(flag, /, text): pass",
                error="bad_posonly",
            )
        yield Case(
            stub="""
            class BadMethod:
                def f(self, number: int, text: str) -> None: ...
            """,
            runtime="""
            class BadMethod:
                def f(self, n, text): pass
            """,
            error="BadMethod.f",
        )
        yield Case(
            stub="""
            class GoodDunder:
                def __exit__(self, t, v, tb) -> None: ...
            """,
            runtime="""
            class GoodDunder:
                def __exit__(self, exc_type, exc_val, exc_tb): pass
            """,
            error=None,
        )

    @collect_cases
    def test_arg_kind(self) -> Iterator[Case]:
        yield Case(
            stub="def runtime_kwonly(number: int, text: str) -> None: ...",
            runtime="def runtime_kwonly(number, *, text): pass",
            error="runtime_kwonly",
        )
        yield Case(
            stub="def stub_kwonly(number: int, *, text: str) -> None: ...",
            runtime="def stub_kwonly(number, text): pass",
            error="stub_kwonly",
        )
        yield Case(
            stub="def stub_posonly(__number: int, text: str) -> None: ...",
            runtime="def stub_posonly(number, text): pass",
            error="stub_posonly",
        )
        if sys.version_info >= (3, 8):
            yield Case(
                stub="def good_posonly(__number: int, text: str) -> None: ...",
                runtime="def good_posonly(number, /, text): pass",
                error=None,
            )
            yield Case(
                stub="def runtime_posonly(number: int, text: str) -> None: ...",
                runtime="def runtime_posonly(number, /, text): pass",
                error="runtime_posonly",
            )
            yield Case(
                stub="def stub_posonly_570(number: int, /, text: str) -> None: ...",
                runtime="def stub_posonly_570(number, text): pass",
                error="stub_posonly_570",
            )

    @collect_cases
    def test_default_value(self) -> Iterator[Case]:
        yield Case(
            stub="def f1(text: str = ...) -> None: ...",
            runtime="def f1(text = 'asdf'): pass",
            error=None,
        )
        yield Case(
            stub="def f2(text: str = ...) -> None: ...", runtime="def f2(text): pass", error="f2"
        )
        yield Case(
            stub="def f3(text: str) -> None: ...",
            runtime="def f3(text = 'asdf'): pass",
            error="f3",
        )
        yield Case(
            stub="def f4(text: str = ...) -> None: ...",
            runtime="def f4(text = None): pass",
            error="f4",
        )
        yield Case(
            stub="def f5(data: bytes = ...) -> None: ...",
            runtime="def f5(data = 'asdf'): pass",
            error="f5",
        )
        yield Case(
            stub="""
            from typing import TypeVar
            _T = TypeVar("_T", bound=str)
            def f6(text: _T = ...) -> None: ...
            """,
            runtime="def f6(text = None): pass",
            error="f6",
        )

    @collect_cases
    def test_static_class_method(self) -> Iterator[Case]:
        yield Case(
            stub="""
            class Good:
                @classmethod
                def f(cls, number: int, text: str) -> None: ...
            """,
            runtime="""
            class Good:
                @classmethod
                def f(cls, number, text): pass
            """,
            error=None,
        )
        yield Case(
            stub="""
            class Bad1:
                def f(cls, number: int, text: str) -> None: ...
            """,
            runtime="""
            class Bad1:
                @classmethod
                def f(cls, number, text): pass
            """,
            error="Bad1.f",
        )
        yield Case(
            stub="""
            class Bad2:
                @classmethod
                def f(cls, number: int, text: str) -> None: ...
            """,
            runtime="""
            class Bad2:
                @staticmethod
                def f(self, number, text): pass
            """,
            error="Bad2.f",
        )
        yield Case(
            stub="""
            class Bad3:
                @staticmethod
                def f(cls, number: int, text: str) -> None: ...
            """,
            runtime="""
            class Bad3:
                @classmethod
                def f(self, number, text): pass
            """,
            error="Bad3.f",
        )
        yield Case(
            stub="""
            class GoodNew:
                def __new__(cls, *args, **kwargs): ...
            """,
            runtime="""
            class GoodNew:
                def __new__(cls, *args, **kwargs): pass
            """,
            error=None,
        )

    @collect_cases
    def test_arg_mismatch(self) -> Iterator[Case]:
        yield Case(
            stub="def f1(a, *, b, c) -> None: ...", runtime="def f1(a, *, b, c): pass", error=None
        )
        yield Case(
            stub="def f2(a, *, b) -> None: ...", runtime="def f2(a, *, b, c): pass", error="f2"
        )
        yield Case(
            stub="def f3(a, *, b, c) -> None: ...", runtime="def f3(a, *, b): pass", error="f3"
        )
        yield Case(
            stub="def f4(a, *, b, c) -> None: ...", runtime="def f4(a, b, *, c): pass", error="f4"
        )
        yield Case(
            stub="def f5(a, b, *, c) -> None: ...", runtime="def f5(a, *, b, c): pass", error="f5"
        )

    @collect_cases
    def test_varargs_varkwargs(self) -> Iterator[Case]:
        yield Case(
            stub="def f1(*args, **kwargs) -> None: ...",
            runtime="def f1(*args, **kwargs): pass",
            error=None,
        )
        yield Case(
            stub="def f2(*args, **kwargs) -> None: ...",
            runtime="def f2(**kwargs): pass",
            error="f2",
        )
        yield Case(
            stub="def g1(a, b, c, d) -> None: ...", runtime="def g1(a, *args): pass", error=None
        )
        yield Case(
            stub="def g2(a, b, c, d, *args) -> None: ...", runtime="def g2(a): pass", error="g2"
        )
        yield Case(
            stub="def g3(a, b, c, d, *args) -> None: ...",
            runtime="def g3(a, *args): pass",
            error=None,
        )
        yield Case(
            stub="def h1(a) -> None: ...", runtime="def h1(a, b, c, d, *args): pass", error="h1"
        )
        yield Case(
            stub="def h2(a, *args) -> None: ...", runtime="def h2(a, b, c, d): pass", error="h2"
        )
        yield Case(
            stub="def h3(a, *args) -> None: ...",
            runtime="def h3(a, b, c, d, *args): pass",
            error="h3",
        )
        yield Case(
            stub="def j1(a: int, *args) -> None: ...", runtime="def j1(a): pass", error="j1"
        )
        yield Case(
            stub="def j2(a: int) -> None: ...", runtime="def j2(a, *args): pass", error="j2"
        )
        yield Case(
            stub="def j3(a, b, c) -> None: ...", runtime="def j3(a, *args, c): pass", error="j3"
        )
        yield Case(stub="def k1(a, **kwargs) -> None: ...", runtime="def k1(a): pass", error="k1")
        yield Case(
            # In theory an error, but led to worse results in practice
            stub="def k2(a) -> None: ...",
            runtime="def k2(a, **kwargs): pass",
            error=None,
        )
        yield Case(
            stub="def k3(a, b) -> None: ...", runtime="def k3(a, **kwargs): pass", error="k3"
        )
        yield Case(
            stub="def k4(a, *, b) -> None: ...", runtime="def k4(a, **kwargs): pass", error=None
        )
        yield Case(
            stub="def k5(a, *, b) -> None: ...",
            runtime="def k5(a, *, b, c, **kwargs): pass",
            error="k5",
        )
        yield Case(
            stub="def k6(a, *, b, **kwargs) -> None: ...",
            runtime="def k6(a, *, b, c, **kwargs): pass",
            error="k6",
        )

    @collect_cases
    def test_overload(self) -> Iterator[Case]:
        yield Case(
            stub="""
            from typing import overload

            @overload
            def f1(a: int, *, c: int = ...) -> int: ...
            @overload
            def f1(a: int, b: int, c: int = ...) -> str: ...
            """,
            runtime="def f1(a, b = 0, c = 0): pass",
            error=None,
        )
        yield Case(
            stub="""
            @overload
            def f2(a: int, *, c: int = ...) -> int: ...
            @overload
            def f2(a: int, b: int, c: int = ...) -> str: ...
            """,
            runtime="def f2(a, b, c = 0): pass",
            error="f2",
        )
        yield Case(
            stub="""
            @overload
            def f3(a: int) -> int: ...
            @overload
            def f3(a: int, b: str) -> str: ...
            """,
            runtime="def f3(a, b = None): pass",
            error="f3",
        )
        yield Case(
            stub="""
            @overload
            def f4(a: int, *args, b: int, **kwargs) -> int: ...
            @overload
            def f4(a: str, *args, b: int, **kwargs) -> str: ...
            """,
            runtime="def f4(a, *args, b, **kwargs): pass",
            error=None,
        )
        if sys.version_info >= (3, 8):
            yield Case(
                stub="""
                @overload
                def f5(__a: int) -> int: ...
                @overload
                def f5(__b: str) -> str: ...
                """,
                runtime="def f5(x, /): pass",
                error=None,
            )

    @collect_cases
    def test_property(self) -> Iterator[Case]:
        yield Case(
            stub="""
            class Good:
                @property
                def read_only_attr(self) -> int: ...
            """,
            runtime="""
            class Good:
                @property
                def read_only_attr(self): return 1
            """,
            error=None,
        )
        yield Case(
            stub="""
            class Bad:
                @property
                def f(self) -> int: ...
            """,
            runtime="""
            class Bad:
                def f(self) -> int: return 1
            """,
            error="Bad.f",
        )
        yield Case(
            stub="""
            class GoodReadOnly:
                @property
                def f(self) -> int: ...
            """,
            runtime="""
            class GoodReadOnly:
                f = 1
            """,
            error=None,
        )
        yield Case(
            stub="""
            class BadReadOnly:
                @property
                def f(self) -> str: ...
            """,
            runtime="""
            class BadReadOnly:
                f = 1
            """,
            error="BadReadOnly.f",
        )
        yield Case(
            stub="""
            class Y:
                @property
                def read_only_attr(self) -> int: ...
                @read_only_attr.setter
                def read_only_attr(self, val: int) -> None: ...
            """,
            runtime="""
            class Y:
                @property
                def read_only_attr(self): return 5
            """,
            error="Y.read_only_attr",
        )
        yield Case(
            stub="""
            class Z:
                @property
                def read_write_attr(self) -> int: ...
                @read_write_attr.setter
                def read_write_attr(self, val: int) -> None: ...
            """,
            runtime="""
            class Z:
                @property
                def read_write_attr(self): return self._val
                @read_write_attr.setter
                def read_write_attr(self, val): self._val = val
            """,
            error=None,
        )
        yield Case(
            stub="""
            class FineAndDandy:
                @property
                def attr(self) -> int: ...
            """,
            runtime="""
            class _EvilDescriptor:
                def __get__(self, instance, ownerclass=None):
                    if instance is None:
                        raise AttributeError('no')
                    return 42
                def __set__(self, instance, value):
                    raise AttributeError('no')

            class FineAndDandy:
                attr = _EvilDescriptor()
            """,
            error=None,
        )

    @collect_cases
    def test_var(self) -> Iterator[Case]:
        yield Case(stub="x1: int", runtime="x1 = 5", error=None)
        yield Case(stub="x2: str", runtime="x2 = 5", error="x2")
        yield Case("from typing import Tuple", "", None)  # dummy case
        yield Case(
            stub="""
            x3: Tuple[int, int]
            """,
            runtime="x3 = (1, 3)",
            error=None,
        )
        yield Case(
            stub="""
            x4: Tuple[int, int]
            """,
            runtime="x4 = (1, 3, 5)",
            error="x4",
        )
        yield Case(stub="x5: int", runtime="def x5(a, b): pass", error="x5")
        yield Case(
            stub="def foo(a: int, b: int) -> None: ...\nx6 = foo",
            runtime="def foo(a, b): pass\ndef x6(c, d): pass",
            error="x6",
        )
        yield Case(
            stub="""
            class X:
                f: int
            """,
            runtime="""
            class X:
                def __init__(self):
                    self.f = "asdf"
            """,
            error=None,
        )
        yield Case(
            stub="""
            class Y:
                read_only_attr: int
            """,
            runtime="""
            class Y:
                @property
                def read_only_attr(self): return 5
            """,
            error="Y.read_only_attr",
        )
        yield Case(
            stub="""
            class Z:
                read_write_attr: int
            """,
            runtime="""
            class Z:
                @property
                def read_write_attr(self): return self._val
                @read_write_attr.setter
                def read_write_attr(self, val): self._val = val
            """,
            error=None,
        )

    @collect_cases
    def test_type_alias(self) -> Iterator[Case]:
        yield Case(
            stub="""
            class X:
                def f(self) -> None: ...
            Y = X
            """,
            runtime="""
            class X:
                def f(self) -> None: ...
            class Y: ...
            """,
            error="Y.f",
        )
        yield Case(
            stub="""
            from typing import Tuple
            A = Tuple[int, str]
            """,
            runtime="A = (int, str)",
            error="A",
        )
        # Error if an alias isn't present at runtime...
        yield Case(stub="B = str", runtime="", error="B")
        # ... but only if the alias isn't private
        yield Case(stub="_C = int", runtime="", error=None)
        yield Case(
            stub="""
            from typing import Tuple
            D = tuple[str, str]
            E = Tuple[int, int, int]
            F = Tuple[str, int]
            """,
            runtime="""
            from typing import List, Tuple
            D = Tuple[str, str]
            E = Tuple[int, int, int]
            F = List[str]
            """,
            error="F",
        )
        yield Case(
            stub="""
            from typing import Union
            G = str | int
            H = Union[str, bool]
            I = str | int
            """,
            runtime="""
            from typing import Union
            G = Union[str, int]
            H = Union[str, bool]
            I = str
            """,
            error="I",
        )
        yield Case(
            stub="""
            import typing
            from collections.abc import Iterable
            from typing import Dict
            K = dict[str, str]
            L = Dict[int, int]
            KK = Iterable[str]
            LL = typing.Iterable[str]
            """,
            runtime="""
            from typing import Iterable, Dict
            K = Dict[str, str]
            L = Dict[int, int]
            KK = Iterable[str]
            LL = Iterable[str]
            """,
            error=None,
        )
        yield Case(
            stub="""
            from typing import Generic, TypeVar
            _T = TypeVar("_T")
            class _Spam(Generic[_T]):
                def foo(self) -> None: ...
            IntFood = _Spam[int]
            """,
            runtime="""
            from typing import Generic, TypeVar
            _T = TypeVar("_T")
            class _Bacon(Generic[_T]):
                def foo(self, arg): pass
            IntFood = _Bacon[int]
            """,
            error="IntFood.foo",
        )
        yield Case(stub="StrList = list[str]", runtime="StrList = ['foo', 'bar']", error="StrList")
        yield Case(
            stub="""
            import collections.abc
            from typing import Callable
            N = Callable[[str], bool]
            O = collections.abc.Callable[[int], str]
            P = Callable[[str], bool]
            """,
            runtime="""
            from typing import Callable
            N = Callable[[str], bool]
            O = Callable[[int], str]
            P = int
            """,
            error="P",
        )
        yield Case(
            stub="""
            class Foo:
                class Bar: ...
            BarAlias = Foo.Bar
            """,
            runtime="""
            class Foo:
                class Bar: pass
            BarAlias = Foo.Bar
            """,
            error=None,
        )
        yield Case(
            stub="""
            from io import StringIO
            StringIOAlias = StringIO
            """,
            runtime="""
            from _io import StringIO
            StringIOAlias = StringIO
            """,
            error=None,
        )
        yield Case(
            stub="""
            from typing import Match
            M = Match[str]
            """,
            runtime="""
            from typing import Match
            M = Match[str]
            """,
            error=None,
        )
        yield Case(
            stub="""
            class Baz:
                def fizz(self) -> None: ...
            BazAlias = Baz
            """,
            runtime="""
            class Baz:
                def fizz(self): pass
            BazAlias = Baz
            Baz.__name__ = Baz.__qualname__ = Baz.__module__ = "New"
            """,
            error=None,
        )
        yield Case(
            stub="""
            class FooBar:
                __module__: None  # type: ignore
                def fizz(self) -> None: ...
            FooBarAlias = FooBar
            """,
            runtime="""
            class FooBar:
                def fizz(self): pass
            FooBarAlias = FooBar
            FooBar.__module__ = None
            """,
            error=None,
        )
        if sys.version_info >= (3, 10):
            yield Case(
                stub="""
                import collections.abc
                import re
                from typing import Callable, Dict, Match, Iterable, Tuple, Union
                Q = Dict[str, str]
                R = dict[int, int]
                S = Tuple[int, int]
                T = tuple[str, str]
                U = int | str
                V = Union[int, str]
                W = Callable[[str], bool]
                Z = collections.abc.Callable[[str], bool]
                QQ = Iterable[str]
                RR = collections.abc.Iterable[str]
                MM = Match[str]
                MMM = re.Match[str]
                """,
                runtime="""
                from collections.abc import Callable, Iterable
                from re import Match
                Q = dict[str, str]
                R = dict[int, int]
                S = tuple[int, int]
                T = tuple[str, str]
                U = int | str
                V = int | str
                W = Callable[[str], bool]
                Z = Callable[[str], bool]
                QQ = Iterable[str]
                RR = Iterable[str]
                MM = Match[str]
                MMM = Match[str]
                """,
                error=None,
            )

    @collect_cases
    def test_enum(self) -> Iterator[Case]:
        yield Case(
            stub="""
            import enum
            class X(enum.Enum):
                a: int
                b: str
                c: str
            """,
            runtime="""
            import enum
            class X(enum.Enum):
                a = 1
                b = "asdf"
                c = 2
            """,
            error="X.c",
        )

    @collect_cases
    def test_decorator(self) -> Iterator[Case]:
        yield Case(
            stub="""
            from typing import Any, Callable
            def decorator(f: Callable[[], int]) -> Callable[..., Any]: ...
            @decorator
            def f() -> Any: ...
            """,
            runtime="""
            def decorator(f): return f
            @decorator
            def f(): return 3
            """,
            error=None,
        )

    @collect_cases
    def test_all_at_runtime_not_stub(self) -> Iterator[Case]:
        yield Case(
            stub="Z: int",
            runtime="""
            __all__ = []
            Z = 5""",
            error=None,
        )

    @collect_cases
    def test_all_in_stub_not_at_runtime(self) -> Iterator[Case]:
        yield Case(stub="__all__ = ()", runtime="", error="__all__")

    @collect_cases
    def test_all_in_stub_different_to_all_at_runtime(self) -> Iterator[Case]:
        # We *should* emit an error with the module name itself,
        # if the stub *does* define __all__,
        # but the stub's __all__ is inconsistent with the runtime's __all__
        yield Case(
            stub="""
            __all__ = ['foo']
            foo: str
            """,
            runtime="""
            __all__ = []
            foo = 'foo'
            """,
            error="",
        )

    @collect_cases
    def test_missing(self) -> Iterator[Case]:
        yield Case(stub="x = 5", runtime="", error="x")
        yield Case(stub="def f(): ...", runtime="", error="f")
        yield Case(stub="class X: ...", runtime="", error="X")
        yield Case(
            stub="""
            from typing import overload
            @overload
            def h(x: int): ...
            @overload
            def h(x: str): ...
            """,
            runtime="",
            error="h",
        )
        yield Case(stub="", runtime="__all__ = []", error=None)  # dummy case
        yield Case(stub="", runtime="__all__ += ['y']\ny = 5", error="y")
        yield Case(stub="", runtime="__all__ += ['g']\ndef g(): pass", error="g")
        # Here we should only check that runtime has B, since the stub explicitly re-exports it
        yield Case(
            stub="from mystery import A, B as B, C as D  # type: ignore", runtime="", error="B"
        )
        yield Case(
            stub="class Y: ...",
            runtime="__all__ += ['Y']\nclass Y:\n  def __or__(self, other): return self|other",
            error="Y.__or__",
        )
        yield Case(
            stub="class Z: ...",
            runtime="__all__ += ['Z']\nclass Z:\n  def __reduce__(self): return (Z,)",
            error=None,
        )

    @collect_cases
    def test_missing_no_runtime_all(self) -> Iterator[Case]:
        yield Case(stub="", runtime="import sys", error=None)
        yield Case(stub="", runtime="def g(): ...", error="g")
        yield Case(stub="", runtime="CONSTANT = 0", error="CONSTANT")

    @collect_cases
    def test_non_public_1(self) -> Iterator[Case]:
        yield Case(
            stub="__all__: list[str]", runtime="", error=f"{TEST_MODULE_NAME}.__all__"
        )  # dummy case
        yield Case(stub="_f: int", runtime="def _f(): ...", error="_f")

    @collect_cases
    def test_non_public_2(self) -> Iterator[Case]:
        yield Case(stub="__all__: list[str] = ['f']", runtime="__all__ = ['f']", error=None)
        yield Case(stub="f: int", runtime="def f(): ...", error="f")
        yield Case(stub="g: int", runtime="def g(): ...", error="g")

    @collect_cases
    def test_dunders(self) -> Iterator[Case]:
        yield Case(
            stub="class A:\n  def __init__(self, a: int, b: int) -> None: ...",
            runtime="class A:\n  def __init__(self, a, bx): pass",
            error="A.__init__",
        )
        yield Case(
            stub="class B:\n  def __call__(self, c: int, d: int) -> None: ...",
            runtime="class B:\n  def __call__(self, c, dx): pass",
            error="B.__call__",
        )
        yield Case(
            stub=(
                "class C:\n"
                "  def __init_subclass__(\n"
                "    cls, e: int = ..., **kwargs: int\n"
                "  ) -> None: ...\n"
            ),
            runtime="class C:\n  def __init_subclass__(cls, e=1, **kwargs): pass",
            error=None,
        )
        if sys.version_info >= (3, 9):
            yield Case(
                stub="class D:\n  def __class_getitem__(cls, type: type) -> type: ...",
                runtime="class D:\n  def __class_getitem__(cls, type): ...",
                error=None,
            )

    @collect_cases
    def test_not_subclassable(self) -> Iterator[Case]:
        yield Case(
            stub="class CanBeSubclassed: ...", runtime="class CanBeSubclassed: ...", error=None
        )
        yield Case(
            stub="class CannotBeSubclassed:\n  def __init_subclass__(cls) -> None: ...",
            runtime="class CannotBeSubclassed:\n  def __init_subclass__(cls): raise TypeError",
            error="CannotBeSubclassed",
        )

    @collect_cases
    def test_name_mangling(self) -> Iterator[Case]:
        yield Case(
            stub="""
            class X:
                def __mangle_good(self, text: str) -> None: ...
                def __mangle_bad(self, number: int) -> None: ...
            """,
            runtime="""
            class X:
                def __mangle_good(self, text): pass
                def __mangle_bad(self, text): pass
            """,
            error="X.__mangle_bad",
        )

    @collect_cases
    def test_mro(self) -> Iterator[Case]:
        yield Case(
            stub="""
            class A:
                def foo(self, x: int) -> None: ...
            class B(A):
                pass
            class C(A):
                pass
            """,
            runtime="""
            class A:
                def foo(self, x: int) -> None: ...
            class B(A):
                def foo(self, x: int) -> None: ...
            class C(A):
                def foo(self, y: int) -> None: ...
            """,
            error="C.foo",
        )
        yield Case(
            stub="""
            class X: ...
            """,
            runtime="""
            class X:
                def __init__(self, x): pass
            """,
            error="X.__init__",
        )

    @collect_cases
    def test_good_literal(self) -> Iterator[Case]:
        yield Case(
            stub=r"""
            from typing_extensions import Literal

            import enum
            class Color(enum.Enum):
                RED: int

            NUM: Literal[1]
            CHAR: Literal['a']
            FLAG: Literal[True]
            NON: Literal[None]
            BYT1: Literal[b'abc']
            BYT2: Literal[b'\x90']
            ENUM: Literal[Color.RED]
            """,
            runtime=r"""
            import enum
            class Color(enum.Enum):
                RED = 3

            NUM = 1
            CHAR = 'a'
            NON = None
            FLAG = True
            BYT1 = b"abc"
            BYT2 = b'\x90'
            ENUM = Color.RED
            """,
            error=None,
        )

    @collect_cases
    def test_bad_literal(self) -> Iterator[Case]:
        yield Case("from typing_extensions import Literal", "", None)  # dummy case
        yield Case(
            stub="INT_FLOAT_MISMATCH: Literal[1]",
            runtime="INT_FLOAT_MISMATCH = 1.0",
            error="INT_FLOAT_MISMATCH",
        )
        yield Case(stub="WRONG_INT: Literal[1]", runtime="WRONG_INT = 2", error="WRONG_INT")
        yield Case(stub="WRONG_STR: Literal['a']", runtime="WRONG_STR = 'b'", error="WRONG_STR")
        yield Case(
            stub="BYTES_STR_MISMATCH: Literal[b'value']",
            runtime="BYTES_STR_MISMATCH = 'value'",
            error="BYTES_STR_MISMATCH",
        )
        yield Case(
            stub="STR_BYTES_MISMATCH: Literal['value']",
            runtime="STR_BYTES_MISMATCH = b'value'",
            error="STR_BYTES_MISMATCH",
        )
        yield Case(
            stub="WRONG_BYTES: Literal[b'abc']",
            runtime="WRONG_BYTES = b'xyz'",
            error="WRONG_BYTES",
        )
        yield Case(
            stub="WRONG_BOOL_1: Literal[True]",
            runtime="WRONG_BOOL_1 = False",
            error="WRONG_BOOL_1",
        )
        yield Case(
            stub="WRONG_BOOL_2: Literal[False]",
            runtime="WRONG_BOOL_2 = True",
            error="WRONG_BOOL_2",
        )

    @collect_cases
    def test_special_subtype(self) -> Iterator[Case]:
        yield Case(
            stub="""
            b1: bool
            b2: bool
            b3: bool
            """,
            runtime="""
            b1 = 0
            b2 = 1
            b3 = 2
            """,
            error="b3",
        )
        yield Case(
            stub="""
            from typing_extensions import TypedDict

            class _Options(TypedDict):
                a: str
                b: int

            opt1: _Options
            opt2: _Options
            opt3: _Options
            """,
            runtime="""
            opt1 = {"a": "3.", "b": 14}
            opt2 = {"some": "stuff"}  # false negative
            opt3 = 0
            """,
            error="opt3",
        )

    @collect_cases
    def test_protocol(self) -> Iterator[Case]:
        yield Case(
            stub="""
            from typing_extensions import Protocol

            class X(Protocol):
                bar: int
                def foo(self, x: int, y: bytes = ...) -> str: ...
            """,
            runtime="""
            from typing_extensions import Protocol

            class X(Protocol):
                bar: int
                def foo(self, x: int, y: bytes = ...) -> str: ...
            """,
            error=None,
        )

    @collect_cases
    def test_type_var(self) -> Iterator[Case]:
        yield Case(
            stub="from typing import TypeVar", runtime="from typing import TypeVar", error=None
        )
        yield Case(stub="A = TypeVar('A')", runtime="A = TypeVar('A')", error=None)
        yield Case(stub="B = TypeVar('B')", runtime="B = 5", error="B")
        if sys.version_info >= (3, 10):
            yield Case(
                stub="from typing import ParamSpec",
                runtime="from typing import ParamSpec",
                error=None,
            )
            yield Case(stub="C = ParamSpec('C')", runtime="C = ParamSpec('C')", error=None)

    @collect_cases
    def test_metaclass_match(self) -> Iterator[Case]:
        yield Case(stub="class Meta(type): ...", runtime="class Meta(type): ...", error=None)
        yield Case(stub="class A0: ...", runtime="class A0: ...", error=None)
        yield Case(
            stub="class A1(metaclass=Meta): ...",
            runtime="class A1(metaclass=Meta): ...",
            error=None,
        )
        yield Case(stub="class A2: ...", runtime="class A2(metaclass=Meta): ...", error="A2")
        yield Case(stub="class A3(metaclass=Meta): ...", runtime="class A3: ...", error="A3")

        # Explicit `type` metaclass can always be added in any part:
        yield Case(
            stub="class T1(metaclass=type): ...",
            runtime="class T1(metaclass=type): ...",
            error=None,
        )
        yield Case(stub="class T2: ...", runtime="class T2(metaclass=type): ...", error=None)
        yield Case(stub="class T3(metaclass=type): ...", runtime="class T3: ...", error=None)

        # Explicit check that `_protected` names are also supported:
        yield Case(stub="class _P1(type): ...", runtime="class _P1(type): ...", error=None)
        yield Case(stub="class P2: ...", runtime="class P2(metaclass=_P1): ...", error="P2")

        # With inheritance:
        yield Case(
            stub="""
            class I1(metaclass=Meta): ...
            class S1(I1): ...
            """,
            runtime="""
            class I1(metaclass=Meta): ...
            class S1(I1): ...
            """,
            error=None,
        )
        yield Case(
            stub="""
            class I2(metaclass=Meta): ...
            class S2: ...  # missing inheritance
            """,
            runtime="""
            class I2(metaclass=Meta): ...
            class S2(I2): ...
            """,
            error="S2",
        )

    @collect_cases
    def test_metaclass_abcmeta(self) -> Iterator[Case]:
        # Handling abstract metaclasses is special:
        yield Case(stub="from abc import ABCMeta", runtime="from abc import ABCMeta", error=None)
        yield Case(
            stub="class A1(metaclass=ABCMeta): ...",
            runtime="class A1(metaclass=ABCMeta): ...",
            error=None,
        )
        # Stubs cannot miss abstract metaclass:
        yield Case(stub="class A2: ...", runtime="class A2(metaclass=ABCMeta): ...", error="A2")
        # But, stubs can add extra abstract metaclass, this might be a typing hack:
        yield Case(stub="class A3(metaclass=ABCMeta): ...", runtime="class A3: ...", error=None)

    @collect_cases
    def test_abstract_methods(self) -> Iterator[Case]:
        yield Case(
            stub="from abc import abstractmethod",
            runtime="from abc import abstractmethod",
            error=None,
        )
        yield Case(
            stub="""
            class A1:
                def some(self) -> None: ...
            """,
            runtime="""
            class A1:
                @abstractmethod
                def some(self) -> None: ...
            """,
            error="A1.some",
        )
        yield Case(
            stub="""
            class A2:
                @abstractmethod
                def some(self) -> None: ...
            """,
            runtime="""
            class A2:
                @abstractmethod
                def some(self) -> None: ...
            """,
            error=None,
        )
        # Runtime can miss `@abstractmethod`:
        yield Case(
            stub="""
            class A3:
                @abstractmethod
                def some(self) -> None: ...
            """,
            runtime="""
            class A3:
                def some(self) -> None: ...
            """,
            error=None,
        )

    @collect_cases
    def test_abstract_properties(self) -> Iterator[Case]:
        # TODO: test abstract properties with setters
        yield Case(
            stub="from abc import abstractmethod",
            runtime="from abc import abstractmethod",
            error=None,
        )
        # Ensure that `@property` also can be abstract:
        yield Case(
            stub="""
            class AP1:
                @property
                def some(self) -> int: ...
            """,
            runtime="""
            class AP1:
                @property
                @abstractmethod
                def some(self) -> int: ...
            """,
            error="AP1.some",
        )
        yield Case(
            stub="""
            class AP1_2:
                def some(self) -> int: ...  # missing `@property` decorator
            """,
            runtime="""
            class AP1_2:
                @property
                @abstractmethod
                def some(self) -> int: ...
            """,
            error="AP1_2.some",
        )
        yield Case(
            stub="""
            class AP2:
                @property
                @abstractmethod
                def some(self) -> int: ...
            """,
            runtime="""
            class AP2:
                @property
                @abstractmethod
                def some(self) -> int: ...
            """,
            error=None,
        )
        # Runtime can miss `@abstractmethod`:
        yield Case(
            stub="""
            class AP3:
                @property
                @abstractmethod
                def some(self) -> int: ...
            """,
            runtime="""
            class AP3:
                @property
                def some(self) -> int: ...
            """,
            error=None,
        )


def remove_color_code(s: str) -> str:
    return re.sub("\\x1b.*?m", "", s)  # this works!


class StubtestMiscUnit(unittest.TestCase):
    def test_output(self) -> None:
        output = run_stubtest(
            stub="def bad(number: int, text: str) -> None: ...",
            runtime="def bad(num, text): pass",
            options=[],
        )
        expected = (
            f'error: {TEST_MODULE_NAME}.bad is inconsistent, stub argument "number" differs '
            'from runtime argument "num"\n'
            f"Stub: at line 1 in file {TEST_MODULE_NAME}.pyi\n"
            "def (number: builtins.int, text: builtins.str)\n"
            f"Runtime: at line 1 in file {TEST_MODULE_NAME}.py\ndef (num, text)\n\n"
            "Found 1 error (checked 1 module)\n"
        )
        assert remove_color_code(output) == expected

        output = run_stubtest(
            stub="def bad(number: int, text: str) -> None: ...",
            runtime="def bad(num, text): pass",
            options=["--concise"],
        )
        expected = (
            "{}.bad is inconsistent, "
            'stub argument "number" differs from runtime argument "num"\n'.format(TEST_MODULE_NAME)
        )
        assert remove_color_code(output) == expected

    def test_ignore_flags(self) -> None:
        output = run_stubtest(
            stub="", runtime="__all__ = ['f']\ndef f(): pass", options=["--ignore-missing-stub"]
        )
        assert output == "Success: no issues found in 1 module\n"

        output = run_stubtest(stub="", runtime="def f(): pass", options=["--ignore-missing-stub"])
        assert output == "Success: no issues found in 1 module\n"

        output = run_stubtest(
            stub="def f(__a): ...", runtime="def f(a): pass", options=["--ignore-positional-only"]
        )
        assert output == "Success: no issues found in 1 module\n"

    def test_allowlist(self) -> None:
        # Can't use this as a context because Windows
        allowlist = tempfile.NamedTemporaryFile(mode="w+", delete=False)
        try:
            with allowlist:
                allowlist.write(f"{TEST_MODULE_NAME}.bad  # comment\n# comment")

            output = run_stubtest(
                stub="def bad(number: int, text: str) -> None: ...",
                runtime="def bad(asdf, text): pass",
                options=["--allowlist", allowlist.name],
            )
            assert output == "Success: no issues found in 1 module\n"

            # test unused entry detection
            output = run_stubtest(stub="", runtime="", options=["--allowlist", allowlist.name])
            assert output == (
                f"note: unused allowlist entry {TEST_MODULE_NAME}.bad\n"
                "Found 1 error (checked 1 module)\n"
            )

            output = run_stubtest(
                stub="",
                runtime="",
                options=["--allowlist", allowlist.name, "--ignore-unused-allowlist"],
            )
            assert output == "Success: no issues found in 1 module\n"

            # test regex matching
            with open(allowlist.name, mode="w+") as f:
                f.write(f"{TEST_MODULE_NAME}.b.*\n")
                f.write("(unused_missing)?\n")
                f.write("unused.*\n")

            output = run_stubtest(
                stub=textwrap.dedent(
                    """
                    def good() -> None: ...
                    def bad(number: int) -> None: ...
                    def also_bad(number: int) -> None: ...
                    """.lstrip(
                        "\n"
                    )
                ),
                runtime=textwrap.dedent(
                    """
                    def good(): pass
                    def bad(asdf): pass
                    def also_bad(asdf): pass
                    """.lstrip(
                        "\n"
                    )
                ),
                options=["--allowlist", allowlist.name, "--generate-allowlist"],
            )
            assert output == (
                f"note: unused allowlist entry unused.*\n" f"{TEST_MODULE_NAME}.also_bad\n"
            )
        finally:
            os.unlink(allowlist.name)

    def test_mypy_build(self) -> None:
        output = run_stubtest(stub="+", runtime="", options=[])
        assert remove_color_code(output) == (
            "error: not checking stubs due to failed mypy compile:\n{}.pyi:1: "
            "error: invalid syntax\n".format(TEST_MODULE_NAME)
        )

        output = run_stubtest(stub="def f(): ...\ndef f(): ...", runtime="", options=[])
        assert remove_color_code(output) == (
            "error: not checking stubs due to mypy build errors:\n{}.pyi:2: "
            'error: Name "f" already defined on line 1\n'.format(TEST_MODULE_NAME)
        )

    def test_missing_stubs(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            test_stubs(parse_options(["not_a_module"]))
        assert remove_color_code(output.getvalue()) == (
            "error: not_a_module failed to find stubs\n"
            "Stub:\nMISSING\nRuntime:\nN/A\n\n"
            "Found 1 error (checked 1 module)\n"
        )

    def test_only_py(self) -> None:
        # in this case, stubtest will check the py against itself
        # this is useful to support packages with a mix of stubs and inline types
        with use_tmp_dir(TEST_MODULE_NAME):
            with open(f"{TEST_MODULE_NAME}.py", "w") as f:
                f.write("a = 1")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                test_stubs(parse_options([TEST_MODULE_NAME]))
            output_str = remove_color_code(output.getvalue())
            assert output_str == "Success: no issues found in 1 module\n"

    def test_get_typeshed_stdlib_modules(self) -> None:
        stdlib = mypy.stubtest.get_typeshed_stdlib_modules(None, (3, 7))
        assert "builtins" in stdlib
        assert "os" in stdlib
        assert "os.path" in stdlib
        assert "asyncio" in stdlib
        assert "graphlib" not in stdlib
        assert "formatter" in stdlib
        assert "contextvars" in stdlib  # 3.7+
        assert "importlib.metadata" not in stdlib

        stdlib = mypy.stubtest.get_typeshed_stdlib_modules(None, (3, 10))
        assert "graphlib" in stdlib
        assert "formatter" not in stdlib
        assert "importlib.metadata" in stdlib

    def test_signature(self) -> None:
        def f(a: int, b: int, *, c: int, d: int = 0, **kwargs: Any) -> None:
            pass

        assert (
            str(mypy.stubtest.Signature.from_inspect_signature(inspect.signature(f)))
            == "def (a, b, *, c, d = ..., **kwargs)"
        )

    def test_config_file(self) -> None:
        runtime = "temp = 5\n"
        stub = "from decimal import Decimal\ntemp: Decimal\n"
        config_file = f"[mypy]\nplugins={root_dir}/test-data/unit/plugins/decimal_to_int.py\n"
        output = run_stubtest(stub=stub, runtime=runtime, options=[])
        assert remove_color_code(output) == (
            f"error: {TEST_MODULE_NAME}.temp variable differs from runtime type Literal[5]\n"
            f"Stub: at line 2 in file {TEST_MODULE_NAME}.pyi\n_decimal.Decimal\nRuntime:\n5\n\n"
            "Found 1 error (checked 1 module)\n"
        )
        output = run_stubtest(stub=stub, runtime=runtime, options=[], config_file=config_file)
        assert output == "Success: no issues found in 1 module\n"

    def test_no_modules(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            test_stubs(parse_options([]))
        assert remove_color_code(output.getvalue()) == "error: no modules to check\n"

    def test_module_and_typeshed(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            test_stubs(parse_options(["--check-typeshed", "some_module"]))
        assert remove_color_code(output.getvalue()) == (
            "error: cannot pass both --check-typeshed and a list of modules\n"
        )
