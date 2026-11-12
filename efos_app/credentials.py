"""Windows Credential Manager backed secret storage.

OAuth access/refresh tokens and the optional low-privilege application role
password are written to the native OS credential vault. They are never written
to PostgreSQL, workspace files, or logs. On platforms without the Windows vault
the store degrades to process memory only (never a plaintext file).
"""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Optional

log = logging.getLogger(__name__)

_TARGET_PREFIX = "EFOS/"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FileTime),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


_PCREDENTIALW = ctypes.POINTER(_CredentialW)


class CredentialVault:
    """Minimal wrapper around CredReadW / CredWriteW / CredDeleteW."""

    _memory_store: dict[str, tuple[str, str]] = {}

    def __init__(self) -> None:
        # Shared process-memory fallback keeps credentials available across
        # vault instances on platforms without the Windows vault.
        self._memory = CredentialVault._memory_store
        self._available = sys.platform == "win32"
        if self._available:
            try:
                self._advapi = ctypes.WinDLL("advapi32", use_last_error=True)
                self._advapi.CredWriteW.argtypes = [_PCREDENTIALW, wintypes.DWORD]
                self._advapi.CredWriteW.restype = wintypes.BOOL
                self._advapi.CredReadW.argtypes = [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    ctypes.POINTER(_PCREDENTIALW),
                ]
                self._advapi.CredReadW.restype = wintypes.BOOL
                self._advapi.CredDeleteW.argtypes = [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                ]
                self._advapi.CredDeleteW.restype = wintypes.BOOL
                self._advapi.CredFree.argtypes = [ctypes.c_void_p]
                self._advapi.CredFree.restype = None
            except (OSError, AttributeError) as exc:  # pragma: no cover - Windows only
                log.warning("Windows credential vault unavailable: %s", exc)
                self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def _target(self, name: str) -> str:
        return name if name.startswith(_TARGET_PREFIX) else _TARGET_PREFIX + name

    def set(self, name: str, username: str, secret: str) -> bool:
        target = self._target(name)
        self._memory[target] = (username, secret)
        if not self._available:
            return False
        try:
            blob = secret.encode("utf-16-le")
            buffer = (ctypes.c_ubyte * max(1, len(blob))).from_buffer_copy(blob or b"\x00")
            cred = _CredentialW()
            cred.Flags = 0
            cred.Type = _CRED_TYPE_GENERIC
            cred.TargetName = target
            cred.Comment = "EFOS protected credential"
            cred.CredentialBlobSize = len(blob)
            cred.CredentialBlob = ctypes.cast(
                buffer, ctypes.POINTER(ctypes.c_ubyte)
            )
            cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
            cred.AttributeCount = 0
            cred.Attributes = None
            cred.TargetAlias = None
            cred.UserName = username or "EFOS"
            ok = bool(self._advapi.CredWriteW(ctypes.byref(cred), 0))
            if not ok:
                log.warning(
                    "Credential vault write failed (winerror=%s)",
                    ctypes.get_last_error(),
                )
            return ok
        except Exception as exc:  # pragma: no cover - Windows only
            log.warning("Credential vault write raised: %s", exc)
            return False

    def get(self, name: str) -> Optional[tuple[str, str]]:
        target = self._target(name)
        if self._available:
            pointer = _PCREDENTIALW()
            try:
                ok = bool(
                    self._advapi.CredReadW(
                        target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)
                    )
                )
                if ok and pointer:
                    cred = pointer.contents
                    size = int(cred.CredentialBlobSize)
                    raw = ctypes.string_at(cred.CredentialBlob, size) if size else b""
                    secret = raw.decode("utf-16-le", errors="ignore")
                    username = cred.UserName or ""
                    return username, secret
            except Exception as exc:  # pragma: no cover - Windows only
                log.warning("Credential vault read raised: %s", exc)
            finally:
                if pointer:
                    try:
                        self._advapi.CredFree(pointer)
                    except Exception:
                        pass
        return self._memory.get(target)

    def delete(self, name: str) -> None:
        target = self._target(name)
        self._memory.pop(target, None)
        if self._available:
            try:
                self._advapi.CredDeleteW(target, _CRED_TYPE_GENERIC, 0)
            except Exception:  # pragma: no cover - Windows only
                pass

    # Convenience aliases used by OAuth and onboarding.
    def set_secret(self, name: str, secret: str, username: str = "EFOS") -> bool:
        return self.set(name, username, secret)

    def get_secret(self, name: str) -> Optional[str]:
        value = self.get(name)
        return value[1] if value else None
