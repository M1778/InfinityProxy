"""Entrypoint: `python -m tenancy` runs the control center (ADR-0011)."""

from __future__ import annotations

import logging

from tenancy.app import create_app
from tenancy.config import TenancySettings

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)


def main() -> None:
    settings = TenancySettings.from_env()
    app = create_app(settings=settings)
    app.run(host=settings.host, port=settings.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
