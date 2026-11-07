"""basis's pytest plugin, which basis-core registers as `pytest11`'s `basis`, so it runs in every repo's tests, from
an IDE's test panel as from `basis check`: a unit test opens no network socket, so none can go flaky by reaching QA.

A test that is not marked `integration` fails when it, or anything it runs, on any thread, connects, binds or sends
a datagram on an IPv4 or IPv6 socket, in its setup, its call or its teardown, whatever the code under test does with
the error it is given, which is no `OSError`, so no client takes it for a network's failure; the socket is closed
first, so it never warns that it was left open. A Unix socket is no network, and stays open to every test, as a
process pool's fork server uses one; and so is the pair of sockets an event loop makes to wake itself, which on
Windows is a connection to 127.0.0.1 (`socket.socketpair`): that pair is all FastAPI's `TestClient` needs, since it
calls the app in the test's process. A test marked `integration`, which the default run leaves out, may open any
socket. A driver that opens its sockets in C, as mssql-python's does, and a DNS lookup are out of the plugin's sight;
the settings a unit test builds with name no real database.

With `--basis-gates FILE`, which `basis check` gives it, the plugin writes there, as JSON, each test that opened a
socket with what it reached, and each test that failed, so that `basis check` prints one `ERROR` line for each test
that opened one (`basis.core.gates`)."""

import json
import socket
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

INTEGRATION = "integration"
"""The marker of a test that may reach a real database or service."""
GUARDED = ("connect", "connect_ex", "bind", "sendto")
"""What a socket does that reaches, or opens itself to, the network."""
_NETWORK = frozenset({socket.AF_INET, socket.AF_INET6})
FIX = "give it a fake of what it reaches, from basis's fakes or the repo's own, or, for a test that needs a real database or service, mark it @pytest.mark.integration"


class SocketBlockedError(RuntimeError):
    """A unit test that opened a network socket; a `RuntimeError`, so that no client takes it for a network's failure
    and answers as if the network were down."""


class Gates:
    """The plugin, as pytest registers it for one session."""

    def __init__(self, config: pytest.Config) -> None:
        self.written: str | None = config.getoption("basis_gates")
        self.blocking = False
        """Whether a unit test is running: read on every thread, as the app a `TestClient` serves runs on another."""
        self.test: str | None = None
        self.pending: list[str] = []
        """What the running phase of the test reached, which its report fails for."""
        self.sockets: dict[str, list[str]] = {}
        self.failed: set[str] = set()
        self._pairing = threading.local()
        self._replaced: dict[str, object] = {}

    def install(self) -> None:
        for name in GUARDED:
            self._replaced[name] = vars(socket.socket).get(name)
            setattr(socket.socket, name, self._guard(name, getattr(socket.socket, name)))
        self._replaced["socketpair"] = socket.socketpair
        socket.socketpair = self._socketpair(socket.socketpair)  # ty: ignore[invalid-assignment] -- the same function, which the wrapper calls with what it is given

    def uninstall(self) -> None:
        for name in GUARDED:
            if (before := self._replaced.get(name)) is None:
                delattr(socket.socket, name)
            else:
                setattr(socket.socket, name, before)
        if (pair := self._replaced.get("socketpair")) is not None:
            socket.socketpair = pair  # ty: ignore[invalid-assignment] -- what it was before install
        self._replaced.clear()

    def _guard(self, name: str, original: Callable[..., object]) -> Callable[..., object]:
        def guarded(sock: socket.socket, *args: object) -> object:
            if self.blocking and sock.family in _NETWORK and not getattr(self._pairing, "on", False):
                reached = f"{name} {_address(args[-1] if name == 'sendto' else args[0])}"
                self.pending.append(reached)
                sock.close()  # which the caller may never do, as `socket.create_connection` closes one only on an OSError
                msg = f"a unit test opened a socket, {reached}, and reaches nothing outside its process: {FIX}"
                raise SocketBlockedError(msg)
            return original(sock, *args)

        return guarded

    def _socketpair(self, original: Callable[..., tuple[socket.socket, socket.socket]]) -> Callable[..., tuple[socket.socket, socket.socket]]:
        def socketpair(*args: object, **kwargs: object) -> tuple[socket.socket, socket.socket]:
            self._pairing.on = True
            try:
                return original(*args, **kwargs)
            finally:
                self._pairing.on = False

        return socketpair

    @contextmanager
    def running(self, item: pytest.Item) -> Generator[None]:
        self.test, self.blocking = item.nodeid, item.get_closest_marker(INTEGRATION) is None
        try:
            yield
        finally:
            self.test, self.blocking = None, False

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, object, object]:  # noqa: ARG002 -- pytest's hook signature
        with self.running(item):
            return (yield)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None, pytest.TestReport, pytest.TestReport]:  # noqa: ARG002 -- pytest's hook signature
        report = yield
        if self.pending:
            self.sockets.setdefault(item.nodeid, []).extend(self.pending)
            if not report.failed:  # the code under test swallowed the error it was given
                report.outcome = "failed"
                report.longrepr = f"{item.nodeid} opened a socket, {', '.join(self.pending)}, and a unit test reaches nothing outside its process: {FIX}"
            self.pending = []
        return report

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.failed:
            self.failed.add(report.nodeid)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:  # noqa: ARG002 -- pytest's hook signature
        if self.written is not None:
            found = {"exit_status": int(exitstatus), "failed": sorted(self.failed), "sockets": self.sockets}
            Path(self.written).write_text(json.dumps(found, indent=2), encoding="utf-8")


def _address(address: object) -> str:
    if isinstance(address, tuple) and len(address) >= 2:  # noqa: PLR2004 -- an IP address's host and port
        host, port = address[0], address[1]
        return f"[{host}]:{port}" if ":" in str(host) else f"{host}:{port}"
    return str(address)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--basis-gates", metavar="FILE", default=None, help="write, as JSON, each test that opened a socket and each that failed, as `basis check` reads them")


def pytest_configure(config: pytest.Config) -> None:
    gates = Gates(config)
    gates.install()
    config.pluginmanager.register(gates, "basis-gates")
    config.add_cleanup(gates.uninstall)
