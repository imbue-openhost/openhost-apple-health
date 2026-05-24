from pathlib import Path
import pytest
from openhost_test_harness import OpenhostStack


@pytest.fixture(scope="session")
def stack():
    with OpenhostStack(app_dir=Path(__file__).resolve().parent.parent) as s:
        yield s
