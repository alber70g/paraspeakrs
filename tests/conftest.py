import pytest


@pytest.fixture(autouse=True)
def _isolated_data_home(monkeypatch, tmp_path_factory):
    """Settings.from_env() moves the pre-0.5.0 data directory into place. Pointed
    at the real ~/.local/share, a test run would move the developer's own jobs."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("xdg-data")))
