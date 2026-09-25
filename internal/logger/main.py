"""File and console logging with govd's logs directory."""

import logging

from internal.config.settings import Settings


def configure_logging(settings: Settings) -> None:
    log_dir = settings.root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(log_dir / "app.log")],
        force=True,
    )
