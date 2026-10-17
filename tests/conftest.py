"""Keep Discord-adapter storage and credentials independent of local installations."""

import pytest


@pytest.fixture(autouse=True)
def isolated_bot_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_ID", raising=False)
