"""Entrypoint: `python -m panel` runs the web dashboard service.

Binds INFINITY_PANEL_HOST:INFINITY_PANEL_PORT (localhost by default,
ADR-0003/ADR-0007) and crawls the engine at INFINITY_ENGINE_URL.
"""

from __future__ import annotations

import logging

from panel.app import create_app
from panel.config import PanelSettings

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)


def main() -> None:
    settings = PanelSettings.from_env()
    app = create_app(settings=settings, poll=True)
    app.run(host=settings.host, port=settings.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
