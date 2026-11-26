"""The ``brain-mapper`` command."""

from __future__ import annotations

import argparse
import signal
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from brain_mapper import checks, flow, package
from brain_mapper.server import WEB, Settings, create_app

DEFAULT_HTTP_PORT = 8740
#: How many ports after the requested one are tried when it is taken.
PORT_TRIES = 100


class _Parser(argparse.ArgumentParser):
    """An argument error under ``--check`` also prints a line that starts with ``FAIL``."""

    check_requested = False

    def error(self, message: str):
        if self.check_requested:
            print(f"FAIL  arguments: {message}", flush=True)
        super().error(message)


def parser(argv: list[str] | None = None) -> argparse.ArgumentParser:
    p = _Parser(
        prog="brain-mapper",
        description="Local demo web app for the open-cognionics headset.",
    )
    p.check_requested = "--check" in (sys.argv[1:] if argv is None else argv)
    p.add_argument("--port", metavar="PATH", help="serial device of the headset")
    p.add_argument("--replay", metavar="PATH", type=Path, help="saved recording to play back")
    p.add_argument(
        "--data-dir",
        metavar="DIR",
        type=Path,
        default=Path.home() / "brain-mapper",
        help="where sessions are kept (default: ~/brain-mapper)",
    )
    p.add_argument(
        "--http-port",
        metavar="N",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help="port on 127.0.0.1; the next free one is used if taken (default: 8740)",
    )
    p.add_argument("--flow", metavar="FILE", type=Path, help="session flow file")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser")
    p.add_argument(
        "--check",
        action="store_true",
        help="print one line per start-up check and exit (0 if all pass, else 1); "
        "opens no port and installs nothing",
    )
    return p


def served(port: int) -> bool:
    """Whether anything accepts a TCP connection on 127.0.0.1:``port`` or [::1]:``port``.

    A wildcard listener (0.0.0.0 or ::) answers on both, so it is found too.
    """
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.3)
                probe.connect((host, port))
                return True
        except OSError:
            continue
    return False


def bind(start: int) -> socket.socket:
    """A listening socket on 127.0.0.1: port ``start`` or the next free one.

    Port 0 lets the operating system choose. A port is skipped when ``served`` finds something
    listening on it; ``SO_REUSEADDR`` (not on Windows) lets a restarted app take back a port whose
    connections are still in TIME_WAIT.
    """
    last: OSError | None = None
    for port in [0] if start == 0 else range(start, min(start + PORT_TRIES, 65536)):
        if port and served(port):
            last = OSError(f"port {port} is in use")
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # A restart takes the same port at once (connections of the run before may still be in
        # TIME_WAIT), so an open page reconnects to it; a port another program listens on is
        # still refused.
        # (Not on Windows, where it would let two programs share the port.)
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            sock.listen(128)
            return sock
        except OSError as error:
            sock.close()
            last = error
    raise OSError(f"no free port from {start}: {last}")


def check(settings: Settings) -> list[str]:
    """Checks of the arguments; returns the problems found (an empty list is a pass).

    Creates nothing. The checks of the installation are ``checks.run``.
    """
    problems = []
    if settings.port is not None and settings.replay is not None:
        problems.append("--port and --replay cannot be used together")
    if not (WEB / "index.html").is_file():
        problems.append(f"page files missing in {WEB}")
    if settings.replay is not None and not settings.replay.exists():
        problems.append(f"--replay path does not exist: {settings.replay}")
    if settings.flow is not None and not settings.flow.is_file():
        problems.append(f"--flow file does not exist: {settings.flow}")
    elif settings.flow is not None:
        try:
            flow.load(settings.flow)
        except (ValueError, OSError) as error:
            problems.append(f"--flow file is not usable: {error}")
    return problems


def open_browser_when_ready(url: str) -> None:
    """Opens the default browser once the server answers ``/api/health``."""

    def run() -> None:
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "/api/health", timeout=1).close()
            except OSError:
                time.sleep(0.1)
                continue
            webbrowser.open(url)
            return

    threading.Thread(target=run, daemon=True).start()


def _interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def recover(data_dir: Path) -> None:
    """Zips the sessions an earlier run left without a zip, with one printed line for each.

    The line names the session folder (whose name holds the label) and nothing else of the session.
    """
    for item in package.recover(data_dir):
        if item.leftovers:
            what = "were removed" if item.removed else "were left as they are, because its zip could not be checked"
            print(
                f"brain-mapper: {item.leftovers} temporary zip file(s) of the session {item.folder}, "
                f"left by an interrupted run, {what}",
                flush=True,
            )
        if item.zip is not None and item.failed_start:
            print(
                f"brain-mapper: the session folder {item.folder} is from a start that failed; "
                "it is kept as it is and saved as a zip now",
                flush=True,
            )
        elif item.zip is not None:
            print(
                f"brain-mapper: the session {item.folder} had not been saved as a zip; it is saved now",
                flush=True,
            )
        if item.late_zip is not None:
            print(
                f"brain-mapper: the session {item.folder} took events after it was zipped; "
                f"an additional zip {item.late_zip} was made beside the first",
                flush=True,
            )
        if item.zip is None and item.problem is not None:
            print(
                f"brain-mapper: the session folder {item.folder} was not zipped ({item.problem}); "
                "it was left as it is",
                flush=True,
            )


def main(argv: list[str] | None = None) -> int:
    args = parser(argv).parse_args(argv)
    settings = Settings(
        data_dir=args.data_dir.expanduser(),
        port=args.port,
        replay=args.replay,
        flow=args.flow,
        http_port=args.http_port,
    )
    problems = check(settings)
    if problems:
        for problem in problems:
            print(f"brain-mapper: {problem}", file=sys.stderr)
            if args.check:
                print(f"FAIL  arguments: {problem}", flush=True)
        return 1
    items = checks.run(settings)
    if args.check:
        # The video host is looked at only here, so the server starts without waiting on the
        # network; it is a warning at most (only the video round needs it).
        for item in [*items, checks.video_host()]:
            print(item.line(), flush=True)
        return 0 if all(item.ok for item in items) else 1
    try:
        (settings.data_dir / "sessions").mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"brain-mapper: cannot create {settings.data_dir / 'sessions'}: {error}", file=sys.stderr)
        return 1
    for item in items:
        if not item.ok:
            print(f"brain-mapper: check failed: {item.line()}", file=sys.stderr, flush=True)
    recover(settings.data_dir)
    sock = bind(settings.http_port)
    settings.http_port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{settings.http_port}"
    app = create_app(settings)
    config = uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=1, lifespan="off")
    server = uvicorn.Server(config)
    # The socket already listens, so a request made right after the line is
    # printed waits in the backlog until the server accepts it.
    print(f"brain-mapper at {url}", flush=True)
    if not args.no_browser:
        open_browser_when_ready(url)
    hub = app.state.hub
    hub.checks = [item.as_dict() for item in items]
    hub.start()
    # uvicorn shuts down on SIGTERM as on Ctrl-C and then raises the signal again; with this
    # handler that ends in the ``except`` below as Ctrl-C does, so the session is zipped first.
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
