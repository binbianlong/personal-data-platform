"""Read dedicated collector credentials from the macOS Keychain."""

from __future__ import annotations

import ctypes
from collections.abc import Mapping

from personal_data_platform.config import ConfigurationError

KEYCHAIN_SERVICE = "personal-data-platform"


def _env_or_keychain(environ: Mapping[str, str], env_name: str, account: str) -> str:
    value = environ.get(env_name, "").strip()
    if value:
        return value
    return _read_keychain(account, env_name)


def _read_keychain(account: str, env_name: str) -> str:
    try:
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    except OSError as error:
        raise ConfigurationError(
            f"{env_name} is required (macOS Keychain is unavailable)"
        ) from error

    find_password = security.SecKeychainFindGenericPassword
    find_password.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    find_password.restype = ctypes.c_int32
    free_content = security.SecKeychainItemFreeContent
    free_content.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    free_content.restype = ctypes.c_int32

    service_bytes = KEYCHAIN_SERVICE.encode("utf-8")
    account_bytes = account.encode("utf-8")
    password_length = ctypes.c_uint32()
    password_data = ctypes.c_void_p()
    try:
        status = find_password(
            None,
            len(service_bytes),
            service_bytes,
            len(account_bytes),
            account_bytes,
            ctypes.byref(password_length),
            ctypes.byref(password_data),
            None,
        )
        if status != 0 or not password_data.value or password_length.value == 0:
            raise ConfigurationError(
                f"{env_name} is required or Keychain account {account!r} must exist"
            )
        try:
            value = ctypes.string_at(password_data, password_length.value).decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise ConfigurationError(f"Keychain account {account!r} must contain UTF-8") from error
        if not value:
            raise ConfigurationError(f"Keychain account {account!r} is empty")
        return value
    finally:
        if password_data.value:
            free_content(None, password_data)
