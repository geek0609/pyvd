from pathlib import Path

import pytest

from internal.config.settings import TWO_GB, _duration, load_settings


def test_govd_env_and_private_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAX_FILE_SIZE", raising=False)
    (tmp_path / "private").mkdir()
    (tmp_path / ".env").write_text(
        "API_ID=123\nAPI_HASH=hash\nBOT_TOKEN=token\nDB_HOST=db\nDB_NAME=govd\n"
        "DB_USER=govd\nMAX_FILE_SIZE=2000 # in MB\nMAX_DURATION=2h30m\n"
    )
    (tmp_path / "private" / "config.yaml").write_text(
        "instagram:\n  disabled: false\n  ignore_regex: ['stories/']\n"
    )
    settings = load_settings(tmp_path)
    assert settings.max_file_size == TWO_GB
    assert settings.max_duration == 9000
    assert settings.site("instagram").ignore_regex[0].search("stories/123")
    assert settings.cookie_path("instagram") == tmp_path / "private/cookies/instagram.txt"


def test_duration_rejects_invalid_value() -> None:
    with pytest.raises(ValueError):
        _duration("2hours")
