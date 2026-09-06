"""Entrypoint: `python -m engine` runs the control plane.

Binds INFINITY_HOST:INFINITY_PORT (localhost by default, ADR-0003).
"""

from __future__ import annotations

import logging

from engine.app import create_app
from engine.config import Settings

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)


def main() -> None:
    settings = Settings.from_env()
    app = create_app(settings=settings, background=True)
    app.run(host=settings.host, port=settings.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
