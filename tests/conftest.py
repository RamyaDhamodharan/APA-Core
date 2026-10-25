import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

CORE_ROOT = Path(__file__).resolve().parents[1]

if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))


@pytest.fixture
def user_stub_factory():
    def build_user(
        role="admin", user_id=None, is_active=True, must_change_password=False
    ):
        return SimpleNamespace(
            id=user_id or uuid.uuid4(),
            role=role,
            is_active=is_active,
            must_change_password=must_change_password,
        )

    return build_user


@pytest.fixture
def user_stub(user_stub_factory):
    return user_stub_factory()
