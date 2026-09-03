"""Run the conversion service.

    python -m converter.web --host 127.0.0.1 --port 8080

The server terminates cleanly on SIGTERM, which is what a container platform
sends when it scales an instance back to zero.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from types import FrameType

from . import Config, create_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT", "8080"))
    )
    args = parser.parse_args(argv)

    config = Config.from_environment()
    server = create_server(args.host, args.port, config)

    def stop(signum: int, frame: FrameType | None) -> None:
        print("shutting down", flush=True)
        server.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print(
        "listening on http://%s:%d  (max upload %d MB, %d conversions/min%s)"
        % (
            args.host,
            args.port,
            config.max_upload_bytes // (1024 * 1024),
            config.requests_per_minute,
            ", token required" if config.access_token else "",
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
