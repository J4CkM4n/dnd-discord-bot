"""Private preparation/game contexts never enter diagnostic payloads or captures."""

from contextlib import contextmanager
from contextvars import ContextVar
import logging

from .debug_capture import capture_context

_private = ContextVar("private_campaign_data", default=False)


def private_data():
    return _private.get()


class _PrivateLogFilter(logging.Filter):
    def filter(self, record):
        return not private_data()


# Application logs contain operator payloads as well as metadata. Private
# operations use durable status records instead; raw exceptions are excluded.
for _logger_name in ("dnd-bot", "discord_adapter"):
    logging.getLogger(_logger_name).addFilter(_PrivateLogFilter())


@contextmanager
def private_context(enabled=True):
    token = _private.set(enabled or _private.get())
    try:
        if _private.get():
            with capture_context(None, ""):
                yield
        else:
            yield
    finally:
        _private.reset(token)
