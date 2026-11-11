"""basis's pytest plugin, as every repo's tests load it: a unit test that opens a network socket fails, whatever the
code under test does with the error, an integration test may open one, and the sockets an event loop and FastAPI's
`TestClient` need stay open to every test. Each run here is a session of its own, through pytester, which loads the
plugin as a repo's pytest does; this workspace's own session leaves it out."""

import json
import socket
import sys

import pytest

INI = "[pytest]\naddopts = -p no:randomly\nmarkers =\n    integration: needs a real database or service\nfilterwarnings = error\n"
UNROUTABLE = '("192.0.2.1", 9)'
"""TEST-NET-1, which no network routes, so a connection the plugin let through would fail, never reach anything."""


@pytest.fixture
def session(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeini(INI)
    return pytester


def test_a_unit_test_that_connects_fails_with_the_fix(session: pytest.Pytester) -> None:
    session.makepyfile(f"import socket\n\n\ndef test_reaches() -> None:\n    socket.create_connection({UNROUTABLE}, timeout=5).close()\n")
    result = session.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*SocketBlockedError: a unit test opened a socket, connect 192.0.2.1:9, and reaches nothing outside its process: give it a fake*"])


def test_a_unit_test_that_listens_fails(session: pytest.Pytester) -> None:
    session.makepyfile("import socket\n\n\ndef test_serves() -> None:\n    with socket.socket() as server:\n        server.bind(('127.0.0.1', 0))\n")
    session.runpytest().assert_outcomes(failed=1)


def test_a_unit_test_fails_even_where_the_code_swallows_the_error(session: pytest.Pytester) -> None:
    session.makepyfile(
        f"import socket\n\n\ndef test_swallows() -> None:\n    with socket.socket() as sock:\n        try:\n            sock.connect({UNROUTABLE})\n        except Exception:\n            pass\n"
    )
    result = session.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*test_swallows opened a socket, connect 192.0.2.1:9, and a unit test reaches nothing outside its process*"])


def test_a_fixture_that_connects_fails_its_test(session: pytest.Pytester) -> None:
    session.makepyfile(
        f"import socket\n\nimport pytest\n\n\n@pytest.fixture\ndef database() -> None:\n    socket.create_connection({UNROUTABLE}, timeout=5).close()\n\n\n"
        "def test_reads(database: None) -> None:\n    pass\n"
    )
    session.runpytest().assert_outcomes(errors=1)


def test_an_integration_test_may_open_sockets(session: pytest.Pytester) -> None:
    session.makepyfile(
        "import socket\n\nimport pytest\n\n\n@pytest.mark.integration\ndef test_serves_and_connects() -> None:\n"
        "    with socket.socket() as server:\n        server.bind(('127.0.0.1', 0))\n        server.listen()\n"
        "        with socket.create_connection(server.getsockname(), timeout=5), server.accept()[0]:\n            pass\n"
    )
    session.runpytest().assert_outcomes(passed=1)


def test_an_event_loop_a_socket_pair_and_unix_sockets_are_no_network(session: pytest.Pytester) -> None:
    session.makepyfile(
        "import asyncio\nimport socket\nimport sys\nfrom pathlib import Path\n\nimport pytest\n\n\n"
        "def test_loop() -> None:\n    asyncio.run(asyncio.sleep(0))\n\n\n"
        "def test_pair() -> None:\n    first, second = socket.socketpair()\n    first.close()\n    second.close()\n\n\n"
        "@pytest.mark.skipif(sys.platform == 'win32', reason='no Unix sockets')\n"
        "def test_unix(tmp_path: Path) -> None:\n    with socket.socket(socket.AF_UNIX) as server:\n        server.bind(str(tmp_path / 's'))\n"
    )
    session.runpytest().assert_outcomes(passed=3) if sys.platform != "win32" else session.runpytest().assert_outcomes(passed=2, skipped=1)


def test_a_test_client_calls_its_app_in_the_process(session: pytest.Pytester) -> None:
    session.makepyfile(
        "from starlette.applications import Starlette\nfrom starlette.responses import PlainTextResponse\nfrom starlette.routing import Route\n"
        "from starlette.testclient import TestClient\n\n\n"
        "def test_calls() -> None:\n    app = Starlette(routes=[Route('/', lambda _: PlainTextResponse('ok'))])\n"
        "    with TestClient(app) as client:\n        assert client.get('/').text == 'ok'\n"
    )
    session.runpytest().assert_outcomes(passed=1)


def test_windows_socket_pair_connects_to_localhost_and_is_let_through(session: pytest.Pytester) -> None:
    """Windows has no native socket pair, and Python makes one over 127.0.0.1, as its fallback does here; the run is a
    process of its own, so that its socket pair does not outlast it."""
    session.makeconftest("import socket\n\nsocket.socketpair = socket._fallback_socketpair\n")
    session.makepyfile("import asyncio\n\n\ndef test_loop() -> None:\n    asyncio.run(asyncio.sleep(0))\n")
    session.runpytest_subprocess().assert_outcomes(passed=1)


def test_basis_check_is_told_each_test_that_opened_a_socket_and_each_that_failed(session: pytest.Pytester) -> None:
    session.makepyfile(f"import socket\n\n\ndef test_reaches() -> None:\n    socket.create_connection({UNROUTABLE}, timeout=5).close()\n\n\ndef test_fails() -> None:\n    raise AssertionError\n")
    written = session.path / "gates.json"
    session.runpytest(f"--basis-gates={written}").assert_outcomes(failed=2)
    assert json.loads(written.read_text(encoding="utf-8")) == {
        "exit_status": 1,
        "failed": [
            "test_basis_check_is_told_each_test_that_opened_a_socket_and_each_that_failed.py::test_fails",
            "test_basis_check_is_told_each_test_that_opened_a_socket_and_each_that_failed.py::test_reaches",
        ],
        "sockets": {"test_basis_check_is_told_each_test_that_opened_a_socket_and_each_that_failed.py::test_reaches": ["connect 192.0.2.1:9"]},
    }


def test_the_plugin_leaves_sockets_as_it_found_them(session: pytest.Pytester) -> None:
    import socket  # noqa: PLC0415 -- the module whose class the plugin patches, as this process has it

    before = {name: vars(socket.socket).get(name) for name in ("connect", "connect_ex", "bind", "sendto")}
    pair = socket.socketpair
    session.makepyfile("def test_nothing() -> None:\n    pass\n")
    session.runpytest().assert_outcomes(passed=1)
    assert {name: vars(socket.socket).get(name) for name in before} == before
    assert socket.socketpair is pair


def test_a_socket_a_thread_opens_after_its_test_ends_is_that_test_s_not_the_running_one(session: pytest.Pytester) -> None:
    session.makepyfile(
        "import socket\nimport threading\n\nSTARTED = threading.Event()\nGO = threading.Event()\nDONE = threading.Event()\n\n\n"
        "def later() -> None:\n    STARTED.set()\n    GO.wait()\n    try:\n        socket.create_connection(" + UNROUTABLE + ", timeout=5).close()\n"
        "    except Exception:\n        pass\n    DONE.set()\n\n\n"
        "def test_leaves_a_thread() -> None:\n    threading.Thread(target=later, name='poller', daemon=True).start()\n    STARTED.wait()\n\n\n"
        "def test_runs_after() -> None:\n    GO.set()\n    DONE.wait(5)\n"
    )
    written = session.path / "gates.json"
    session.runpytest(f"--basis-gates={written}").assert_outcomes(passed=2)
    name = "test_a_socket_a_thread_opens_after_its_test_ends_is_that_test_s_not_the_running_one.py"
    assert json.loads(written.read_text(encoding="utf-8"))["sockets"] == {f"{name}::test_leaves_a_thread": ["connect 192.0.2.1:9, on the thread poller it left running"]}


PROACTOR = "class IocpProactor:\n    def connect(self, conn, address):\n        return 'connected'\n\n    def sendto(self, conn, buf, flags=0, addr=None):\n        return 'sent'\n"
"""A stand-in for Windows' `asyncio.windows_events.IocpProactor`, whose `connect` and `sendto` reach the network
through `_overlapped`, in C, never through `socket.socket`'s methods."""


def test_windows_event_loop_connects_and_sends_through_its_proactor_and_is_blocked(session: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows' default event loop, the proactor, connects with `ConnectEx` and sends a datagram with `WSASendTo`, so
    an `httpx.AsyncClient` reaching QA would pass there and fail in WSL; the plugin guards the proactor too, here on a
    stand-in of its class, which a Windows run checks on the real one below."""
    windows_events = type(pytest)("asyncio.windows_events")
    exec(PROACTOR, vars(windows_events))  # noqa: S102 -- the stand-in's own source, above
    monkeypatch.setitem(__import__("sys").modules, "asyncio.windows_events", windows_events)
    before = dict(vars(windows_events.IocpProactor))
    session.makepyfile(
        "import socket\n\nimport pytest\nfrom asyncio.windows_events import IocpProactor\n\nDGRAM = (socket.AF_INET, socket.SOCK_DGRAM)\n\n\n"
        f"def test_connects() -> None:\n    with socket.socket() as sock:\n        IocpProactor().connect(sock, {UNROUTABLE})\n\n\n"
        f"def test_sends() -> None:\n    with socket.socket(*DGRAM) as sock:\n        IocpProactor().sendto(sock, b'x', addr={UNROUTABLE})\n\n\n"
        "@pytest.mark.skipif(not hasattr(socket, 'AF_UNIX'), reason='no Unix sockets')\n"
        "def test_sends_on_a_unix_socket() -> None:\n    with socket.socket(socket.AF_UNIX) as sock:\n        assert IocpProactor().sendto(sock, b'x', addr='there') == 'sent'\n\n\n"
        "def test_sends_where_it_connected() -> None:\n    with socket.socket(*DGRAM) as sock:\n        assert IocpProactor().sendto(sock, b'x') == 'sent'\n"
    )
    result = session.runpytest()
    result.assert_outcomes(failed=2, passed=2) if hasattr(socket, "AF_UNIX") else result.assert_outcomes(failed=2, passed=1, skipped=1)
    result.stdout.fnmatch_lines(["*SocketBlockedError: a unit test opened a socket, connect 192.0.2.1:9*", "*SocketBlockedError: a unit test opened a socket, sendto 192.0.2.1:9*"])
    assert dict(vars(windows_events.IocpProactor)) == before  # put back as it was


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows' default event loop, the proactor, is Windows' alone")
def test_an_async_client_on_windows_default_event_loop_is_blocked(session: pytest.Pytester) -> None:
    session.makepyfile(
        f"import asyncio\n\n\nasync def reach() -> None:\n    await asyncio.wait_for(asyncio.open_connection(*{UNROUTABLE}), 5)\n\n\ndef test_reaches() -> None:\n    asyncio.run(reach())\n"
    )
    result = session.runpytest_subprocess()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*SocketBlockedError: a unit test opened a socket, connect 192.0.2.1:9*"])
