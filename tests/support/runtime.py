"""Small real-socket servers for gateway integration tests."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn


@contextmanager
def running_server(app, *, startup_timeout: float = 5.0) -> Iterator[str]:
    """Serve an ASGI app on an ephemeral loopback socket with lifespan."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        lifespan="on",
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name=f"test-uvicorn-{port}",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + startup_timeout
    while not server.started and thread.is_alive():
        if time.monotonic() >= deadline:
            server.should_exit = True
            thread.join(timeout=1)
            listener.close()
            raise TimeoutError(f"server on port {port} did not start")
        time.sleep(0.01)
    if not thread.is_alive():
        listener.close()
        raise RuntimeError(f"server on port {port} exited during startup")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=startup_timeout)
        listener.close()
        if thread.is_alive():
            raise TimeoutError(f"server on port {port} did not stop")
