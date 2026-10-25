import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

# storage reads DATA_DIR and webui creates its secret key at import time, so
# point both at a throwaway folder before any app module is imported.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cinematix-test-")

import storage  # noqa: E402


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A fresh, empty data folder for each test."""
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(storage, "RESULTS_FILE", tmp_path / "results.json")
    monkeypatch.setattr(storage, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(storage, "SEATPLANS_FILE", tmp_path / "seatplans.json")
    monkeypatch.setattr(storage, "SECRET_FILE", tmp_path / "secret_key")
    for var in ("CINEMA_ID", "TMDB_API_KEY", "NTFY_TOPIC", "NTFY_URL", "WEBUI_URL"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def set_up_config(data_dir):
    """A config that has completed setup, for Prague Flora."""
    cfg = storage.load_config()
    storage.update_settings(cfg, {
        "cinema_id": "1052",
        "cinema_name": "Prague Flora, OC FLORA",
        "cinema_link": "https://www.cinemacity.cz/cinemas/flora",
        "tmdb_api_key": "key",
        "ntfy_endpoint": "https://ntfy.sh/test-topic",
    })
    return cfg
