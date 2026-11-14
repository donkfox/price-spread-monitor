from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes


_ENTROPY = b"PriceMonitor/license-token/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class SecretProtectionError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = (
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    )


def _input_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return blob, buffer


def _windows_libraries():
    if os.name != "nt":
        raise SecretProtectionError("商业授权令牌只能在 Windows 上安全保存")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _protect(value: str, entropy_value: bytes, description: str) -> str:
    if not value:
        raise SecretProtectionError("授权令牌为空")
    crypt32, kernel32 = _windows_libraries()
    source, source_buffer = _input_blob(value.encode("utf-8"))
    entropy, entropy_buffer = _input_blob(entropy_value)
    output = _DataBlob()
    try:
        if not crypt32.CryptProtectData(
            ctypes.byref(source),
            description,
            ctypes.byref(entropy),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        encrypted = ctypes.string_at(output.pbData, output.cbData)
        return base64.urlsafe_b64encode(encrypted).decode("ascii")
    except OSError as error:
        raise SecretProtectionError("无法安全保存授权令牌") from error
    finally:
        del source_buffer, entropy_buffer
        if output.pbData:
            kernel32.LocalFree(output.pbData)


def _unprotect(value: str, entropy_value: bytes) -> str:
    try:
        encrypted = base64.b64decode(
            value.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, ValueError) as error:
        raise SecretProtectionError("本机授权令牌已损坏") from error
    crypt32, kernel32 = _windows_libraries()
    source, source_buffer = _input_blob(encrypted)
    entropy, entropy_buffer = _input_blob(entropy_value)
    output = _DataBlob()
    description = wintypes.LPWSTR()
    try:
        if not crypt32.CryptUnprotectData(
            ctypes.byref(source),
            ctypes.byref(description),
            ctypes.byref(entropy),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SecretProtectionError("本机授权令牌无效或不属于当前 Windows 用户") from error
    finally:
        del source_buffer, entropy_buffer
        if description:
            kernel32.LocalFree(description)
        if output.pbData:
            kernel32.LocalFree(output.pbData)


def protect_secret(value: str) -> str:
    return _protect(value, _ENTROPY, "Price Monitor license token")


def unprotect_secret(value: str) -> str:
    return _unprotect(value, _ENTROPY)


def protect_named_secret(value: str, purpose: str) -> str:
    if not purpose or not purpose.isascii():
        raise SecretProtectionError("密钥用途无效")
    entropy = f"PriceMonitor/{purpose}/v1".encode("ascii")
    return _protect(value, entropy, f"Price Monitor {purpose}")


def unprotect_named_secret(value: str, purpose: str) -> str:
    if not purpose or not purpose.isascii():
        raise SecretProtectionError("密钥用途无效")
    entropy = f"PriceMonitor/{purpose}/v1".encode("ascii")
    return _unprotect(value, entropy)
