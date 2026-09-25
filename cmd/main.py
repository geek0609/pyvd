"""Launch the bot from the govd-style cmd directory."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from internal.bot.main import run  # noqa: E402


if __name__ == "__main__":
    asyncio.run(run())
