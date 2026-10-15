"""Entry point: the Discord adapter (Discord-only bot, v4 engine)."""

import asyncio
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("dnd-bot")


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


async def main():
    discord_token = env("DISCORD_TOKEN")
    if not discord_token:
        log.error("No DISCORD_TOKEN configured. "
                  "Create a .env file from .env.example.")
        sys.exit(1)

    from adapters.discord_bot import run_discord
    from core.agent_config import ConfigError
    try:
        await run_discord(discord_token)
    except ConfigError as e:
        log.error("Invalid agent configuration: %s", e)
        sys.exit(1)
    except Exception as e:
        log.error("discord adapter crashed: %s", e)
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
