"""One-command start: the app serves on loopback only and checks Origin and Host."""

from __future__ import annotations

import http.client
import json
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect


def lan_address() -> str:
    """The address of this machine on its network (no packet is sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    finally:
        probe.close()
    assert not address.startswith("127."), "this machine has no address besides loopback"
    return address


def health(url: str) -> dict:
    return json.load(urllib.request.urlopen(url + "/api/health"))


def test_one_command_serves_the_page_on_loopback(app, recorded_session, page):
    a = app(replay=recorded_session)
    assert a.url.startswith("http://127.0.0.1:")
    page.goto(a.url)
    assert page.title() == "brain-mapper"
    assert health(a.url)["ok"] is True


def test_the_server_is_not_reachable_on_other_addresses(app, recorded_session):
    a = app(replay=recorded_session)
    _, port = a.host_port
    assert health(a.url)["ok"] is True
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection((lan_address(), port), timeout=3).close()


def test_a_foreign_origin_cannot_open_the_websocket(app, recorded_session):
    a = app(replay=recorded_session)
    host, port = a.host_port
    ws_url = f"ws://{host}:{port}/ws"
    with pytest.raises(InvalidStatus) as refused:
        connect(ws_url, origin="http://example.com")
    assert refused.value.response.status_code == 403
    with pytest.raises(InvalidStatus) as refused:
        connect(ws_url, origin=f"http://127.0.0.1:{port + 1}")
    assert refused.value.response.status_code == 403
    with pytest.raises(InvalidStatus) as refused:
        connect(ws_url, origin=None)
    assert refused.value.response.status_code == 403
    for origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
        with connect(ws_url, origin=origin):
            pass


def test_a_foreign_host_header_is_refused(app, recorded_session):
    a = app(replay=recorded_session)
    host, port = a.host_port

    def status(path, host_header, method="GET", origin=None):
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host_header)
        if origin is not None:
            connection.putheader("Origin", origin)
        connection.putheader("Content-Length", "0")
        connection.endheaders()
        code = connection.getresponse().status
        connection.close()
        return code

    own = f"127.0.0.1:{port}"
    assert status("/api/health", own) == 200
    assert status("/api/health", f"localhost:{port}") == 200
    assert status("/api/health", f"example.com:{port}") == 403
    assert status("/api/health", "example.com") == 403
    assert status("/", f"example.com:{port}") == 403
    # A DNS-rebinding page reaches the server with its own name as Host.
    assert status("/api/health", f"127.0.0.1.example.com:{port}") == 403
    # Every non-GET request needs an allowed Origin as well as an allowed Host.
    assert status("/api/health", own, method="POST") == 403
    assert status("/api/health", own, method="POST", origin="http://example.com") == 403
    assert status("/api/health", own, method="POST", origin=f"http://{own}") != 403


def test_starting_without_port_or_replay_is_refused_by_the_test_helper(app):
    with pytest.raises(ValueError):
        app()
    with pytest.raises(ValueError):
        app(flow="anything.json")


def test_ctrl_c_stops_the_process_within_three_seconds(app, recorded_session):
    a = app(replay=recorded_session)
    assert health(a.url)["ok"] is True
    a.process.send_signal(signal.SIGINT)
    start = time.monotonic()
    a.process.wait(timeout=3)
    assert time.monotonic() - start < 3
    assert a.process.returncode == 0


def test_a_taken_http_port_moves_to_the_next_free_one(app, recorded_session):
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    taken = holder.getsockname()[1]
    try:
        a = app(replay=recorded_session, http_port=taken)
        _, port = a.host_port
        assert taken < port <= taken + 100
        assert health(a.url)["ok"] is True
    finally:
        holder.close()


def test_the_app_starts_on_a_pty_device_path(app, pty_device):
    a = app(port=pty_device.path)
    assert health(a.url)["ok"] is True


def test_port_together_with_replay_is_refused_at_start_up(recorded_session, tmp_path):
    command = Path(sys.executable).parent / "brain-mapper"
    result = subprocess.run(
        [
            str(command),
            "--check",
            "--data-dir",
            str(tmp_path / "data"),
            "--port",
            str(tmp_path / "no-such-device"),
            "--replay",
            str(recorded_session),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "--port and --replay cannot be used together" in result.stderr


# bind(): the port is refused when anything already accepts connections on it, a port just given
# up (connections left in TIME_WAIT) is taken again.


def _listener(host: str, family=socket.AF_INET, port: int = 0) -> socket.socket:
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.bind((host, port))
    sock.listen()
    return sock


def test_bind_moves_on_from_a_port_a_wildcard_listener_serves():
    from brain_mapper.cli import bind

    holder = _listener("0.0.0.0")
    taken = holder.getsockname()[1]
    try:
        sock = bind(taken)
        try:
            assert taken < sock.getsockname()[1] <= taken + 100
        finally:
            sock.close()
    finally:
        holder.close()


def test_bind_moves_on_from_a_port_a_loopback_listener_serves():
    from brain_mapper.cli import bind

    holder = _listener("127.0.0.1")
    taken = holder.getsockname()[1]
    try:
        sock = bind(taken)
        try:
            assert taken < sock.getsockname()[1] <= taken + 100
        finally:
            sock.close()
    finally:
        holder.close()


def test_bind_moves_on_from_a_port_an_ipv6_loopback_listener_serves():
    from brain_mapper.cli import bind

    try:
        holder = _listener("::1", socket.AF_INET6)
    except OSError:
        return  # no IPv6 loopback on this machine: nothing can serve a port there
    taken = holder.getsockname()[1]
    try:
        sock = bind(taken)
        try:
            assert taken < sock.getsockname()[1] <= taken + 100
        finally:
            sock.close()
    finally:
        holder.close()


def test_bind_takes_a_port_back_that_was_just_closed_with_connections_in_time_wait():
    from brain_mapper.cli import bind

    first = _listener("127.0.0.1")
    port = first.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    served, _ = first.accept()
    served.close()  # the side that closes first keeps the TIME_WAIT entry
    client.recv(1)
    client.close()
    first.close()
    sock = bind(port)
    try:
        assert sock.getsockname()[1] == port
    finally:
        sock.close()
