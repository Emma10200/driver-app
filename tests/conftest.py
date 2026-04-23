import pytest
import submission_storage

@pytest.fixture(autouse=True)
def clear_secret_cache():
    submission_storage._get_secret.cache_clear()
