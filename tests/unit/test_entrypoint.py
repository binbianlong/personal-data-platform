import pytest

from personal_data_platform.entrypoint import ROLES, main, resolve_role


@pytest.mark.parametrize("role", sorted(ROLES))
def test_resolve_role_accepts_known_roles(role: str) -> None:
    assert resolve_role(role) == role


def test_resolve_role_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match=r"^unknown role: unsupported$"):
        resolve_role("unsupported")


@pytest.mark.parametrize("role", sorted(ROLES))
def test_main_accepts_known_roles(role: str) -> None:
    assert main([role]) == 0
