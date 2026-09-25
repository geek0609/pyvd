"""Copy govd's per-site cookies for download jobs."""

import shutil
from pathlib import Path

from internal.config.settings import Settings


def job_cookie_file(settings: Settings, extractor_id: str, workdir: Path) -> Path | None:
    source = settings.cookie_path(extractor_id)
    if not source.is_file():
        return None
    target = workdir / "cookies.txt"
    shutil.copyfile(source, target)
    target.chmod(0o600)
    return target
