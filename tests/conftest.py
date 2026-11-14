"""Shared pytest setup. Media helpers live in tests/helpers.py."""
import os

import pytest

from tests.helpers import clear_cache, make_video  # noqa: F401  (make_video re-exported)



@pytest.fixture(scope="session", autouse=True)
def _cleanup_media_cache():
    yield
    clear_cache()


@pytest.fixture(scope="session")
def hardware_encoder():
    """The encoder this machine can really use, found the way ShelfSaver finds it ("auto").

    GPU tests take this instead of assuming NVIDIA, so the same tests run on an NVIDIA, AMD or
    Intel machine. With no working encoder they skip rather than fail.
    """
    from pathlib import Path

    from shelfsaver.config import Config
    from shelfsaver.encode import encoder_works
    from shelfsaver.encoder_choice import EncoderChoice

    found = EncoderChoice(Config(data_dir=Path("."), encoder="auto"),
                          lambda enc: encoder_works("ffmpeg", enc)).after_failures()
    if found is None:
        pytest.skip("no working hardware encoder")
    return found


@pytest.fixture(autouse=True)
def _no_ambient_shelfsaver_settings(monkeypatch):
    """Tests must not depend on the machine they run on.

    load_config() reads SHELFSAVER_* variables, and the Docker image sets several of them
    (media roots, data folder, ...). Run inside that image, or on a PC where someone set one,
    a test that expects the plain default fails for a reason unrelated to the code. Removing them
    all first means each test starts from "nothing configured" and sets what it needs itself.
    """
    for name in [n for n in os.environ if n.startswith("SHELFSAVER_")]:
        monkeypatch.delenv(name)
