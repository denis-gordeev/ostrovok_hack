import pytest

from hotel_agent.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)
