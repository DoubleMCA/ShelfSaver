"""Shared pytest setup. Media helpers live in tests/helpers.py."""
import pytest

from tests.helpers import clear_cache, make_video  # noqa: F401  (make_video re-exported)



@pytest.fixture(scope="session", autouse=True)
def _cleanup_media_cache():
    yield
    clear_cache()
