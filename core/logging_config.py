"""Application debug logging without enabling HTTP or Discord wire dumps."""
import logging
import os


def configure_logging():
    requested = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    level = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
             "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}.get(requested)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for name in ("dnd-bot", "discord_adapter"):
        logging.getLogger(name).setLevel(level if level is not None else logging.INFO)
    # Wire logs can include headers and Discord payloads. App DEBUG is sufficient.
    for name in ("httpx", "httpcore", "discord"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if level is None:
        logging.getLogger("dnd-bot").warning("Invalid LOG_LEVEL; using INFO")
