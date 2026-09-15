"""Run the app under a real uvicorn on 127.0.0.1, so tests speak HTTP to it the way a
boto3 client does -- including chunked transfer encoding, which TestClient does not."""

import socket
import threading
import time
from contextlib import contextmanager

import uvicorn

from filen_s3_emulator.config import Settings
from filen_s3_emulator.main import create_app


@contextmanager
def serve(settings: Settings, upstream):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(
        create_app(settings, upstream), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
