"""Launch the bot from the govd-style cmd directory."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from internal.bot.main import run  # noqa: E402
from internal.config.settings import load_settings  # noqa: E402


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        settings = load_settings()
        print(
            f"Configuration valid: {len(settings.site_configs)} site overrides, "
            f"{settings.max_file_size // 1_000_000} MB file limit"
        )
    else:
        asyncio.run(run())
