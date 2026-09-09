"""Entry point: `python -m hostagent` runs the loopback host control agent."""

from __future__ import annotations

from hostagent.app import create_app
from hostagent.config import HostAgentSettings


def main() -> None:
    settings = HostAgentSettings.from_env()
    app = create_app(settings)
    app.run(host=settings.host, port=settings.port, threaded=True)


if __name__ == "__main__":
    main()
