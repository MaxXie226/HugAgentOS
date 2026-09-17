"""Mandatory integrity labels on the filesystem, the other half of the token.

A low-integrity process may write an object only if the object's own label is
Low. Windows treats an unlabeled object as Medium, so out of the box the sandbox
can write nothing; each writable root gets an inheritable Low label and each
read-only carve-out inside it an explicit Medium one. Explicit beats inherited:
re-labeling the root later leaves the carve-out alone, and the carve-out's own
children inherit Medium from it. Neither label needs a privilege the desktop's
own user lacks — a *protected* SACL would.

A label is read back before it is written, and that is not an optimisation:
``SetNamedSecurityInfoW`` propagates an inheritable entry to every existing
child, so re-applying one that is already there means walking the whole
workspace on every single command.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from . import ffi


def label_writable(path: str) -> None:
    """Let low-integrity processes write ``path`` and everything below it."""
    _ensure(path, _entry(ffi.LOW_INTEGRITY))


def label_read_only(path: str) -> None:
    """Take write access back for ``path`` and below inside a writable root."""
    _ensure(path, _entry(ffi.MEDIUM_INTEGRITY))


def remove_label(path: str) -> None:
    """Undo :func:`label_writable`; a path that no longer exists needs nothing."""
    try:
        current = current_label(path)
    except ffi.Win32Error as error:
        if error.code in (ffi.ERROR_FILE_NOT_FOUND, ffi.ERROR_PATH_NOT_FOUND):
            return
        raise
    if _entry(ffi.LOW_INTEGRITY) in current:
        _write(path, "")


def current_label(path: str) -> str:
    """The path's label as SDDL, e.g. ``S:(ML;OICI;NW;;;LW)``; inherited entries carry ``ID``."""
    descriptor = ctypes.c_void_p()
    ffi.check_status(
        ffi.GetNamedSecurityInfoW(
            path,
            ffi.SE_FILE_OBJECT,
            ffi.LABEL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
            ctypes.byref(descriptor),
        ),
        "GetNamedSecurityInfoW",
    )
    try:
        text = ctypes.c_wchar_p()
        ffi.check(
            ffi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor,
                ffi.SDDL_REVISION_1,
                ffi.LABEL_SECURITY_INFORMATION,
                ctypes.byref(text),
                None,
            ),
            "ConvertSecurityDescriptorToStringSecurityDescriptorW",
        )
        try:
            return text.value or ""
        finally:
            ffi.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        ffi.LocalFree(descriptor)


def _entry(level: str) -> str:
    return f"(ML;OICI;NW;;;{level})"


def _ensure(path: str, entry: str) -> None:
    if entry not in current_label(path):
        _write(path, entry)


def _write(path: str, entry: str) -> None:
    descriptor = ctypes.c_void_p()
    ffi.check(
        ffi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            "S:" + entry, ffi.SDDL_REVISION_1, ctypes.byref(descriptor), None
        ),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW",
    )
    try:
        present = wintypes.BOOL()
        sacl = ctypes.c_void_p()
        defaulted = wintypes.BOOL()
        ffi.check(
            ffi.GetSecurityDescriptorSacl(
                descriptor, ctypes.byref(present), ctypes.byref(sacl), ctypes.byref(defaulted)
            ),
            "GetSecurityDescriptorSacl",
        )
        ffi.check_status(
            ffi.SetNamedSecurityInfoW(
                path, ffi.SE_FILE_OBJECT, ffi.LABEL_SECURITY_INFORMATION, None, None, None, sacl
            ),
            "SetNamedSecurityInfoW",
        )
    finally:
        ffi.LocalFree(descriptor)


__all__ = ["current_label", "label_read_only", "label_writable", "remove_label"]
