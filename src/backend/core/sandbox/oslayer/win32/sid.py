"""Owning a SID allocated by Win32."""

from __future__ import annotations

import ctypes

from . import ffi


class OwnedSid:
    """A SID allocated by Win32 that must be released with ``LocalFree``."""

    def __init__(self, pointer: ctypes.c_void_p) -> None:
        self._pointer: ctypes.c_void_p | None = pointer

    @classmethod
    def from_string(cls, value: str) -> "OwnedSid":
        pointer = ctypes.c_void_p()
        ffi.check(
            ffi.ConvertStringSidToSidW(value, ctypes.byref(pointer)), "ConvertStringSidToSidW"
        )
        return cls(pointer)

    @property
    def pointer(self) -> ctypes.c_void_p:
        if self._pointer is None:
            raise RuntimeError("SID 已释放")
        return self._pointer

    def close(self) -> None:
        if self._pointer is not None:
            ffi.LocalFree(self._pointer)
            self._pointer = None

    def __enter__(self) -> "OwnedSid":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = ["OwnedSid"]
