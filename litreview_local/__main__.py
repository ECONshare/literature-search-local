"""Run LitReview Local: python -m litreview_local [--port N] [--data-dir PATH].

The launcher (local/launcher.py) runs this inside local/.venv; the Claude
Desktop bridge runs it when Claude asks for LitReview while it is closed.
The server never opens a browser itself - whoever started it does that.
"""
import argparse
import logging
import os
import socket
import sys
from logging.handlers import RotatingFileHandler


def _configure_logging(logs_dir) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(logs_dir / "server.log", maxBytes=2_000_000,
                                       backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(console)
    # The access log answers every poll of every open page; keep it quiet.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name != "nt":
            # As uvicorn binds: without it, a restart within a minute of a
            # stop finds the port still in TIME_WAIT on macOS and Linux. Not
            # on Windows, where the same option would let two servers share it.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m litreview_local",
                                     description="Run LitReview Local.")
    parser.add_argument("--port", type=int, help="port to listen on (default 8765)")
    parser.add_argument("--data-dir", help="where the library lives "
                                           "(default ~/LitReview)")
    args = parser.parse_args(argv)
    if args.port:
        os.environ["LITREVIEW_PORT"] = str(args.port)
    if args.data_dir:
        os.environ["LITREVIEW_DATA_DIR"] = args.data_dir

    from .config import settings

    settings.reload_location()
    _configure_logging(settings.logs_dir)
    if not _port_free(settings.port):
        print(f"Port {settings.port} is already in use. If LitReview is already "
              f"running, open {settings.base_url} in your browser. Otherwise "
              "start it on another port with --port, or set LITREVIEW_PORT.",
              file=sys.stderr)
        return 2

    import uvicorn

    from .main import app, set_exit_hook

    config = uvicorn.Config(app, host="127.0.0.1", port=settings.port,
                            log_config=None, access_log=False)
    server = uvicorn.Server(config)
    set_exit_hook(lambda: setattr(server, "should_exit", True))
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
