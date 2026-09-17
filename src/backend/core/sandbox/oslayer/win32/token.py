"""Low-integrity token construction.

The sandbox token is the current process token with every privilege removed
(``CreateRestrictedToken`` with ``DISABLE_MAX_PRIVILEGE``; ``SeChangeNotify`` is
put back because path traversal fails without it) and its integrity level set to
Low. Mandatory integrity control does the confining from there: a Low process
may not write any object labeled higher, and Windows treats an unlabeled object
as Medium, so the token can write exactly the paths the launcher labeled Low —
not the user's profile, not the registry, not another process. Why this replaced
the restricted-token port is recorded in
``internal design docs``.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from . import ffi
from .sid import OwnedSid

CHANGE_NOTIFY_PRIVILEGE = "SeChangeNotifyPrivilege"


def open_current_process_token() -> wintypes.HANDLE:
    """Open this process's token with the rights needed to derive the sandbox token."""
    desired = (
        ffi.TOKEN_DUPLICATE
        | ffi.TOKEN_QUERY
        | ffi.TOKEN_ASSIGN_PRIMARY
        | ffi.TOKEN_ADJUST_DEFAULT
        | ffi.TOKEN_ADJUST_PRIVILEGES
    )
    handle = wintypes.HANDLE()
    ffi.check(
        ffi.OpenProcessToken(ffi.GetCurrentProcess(), desired, ctypes.byref(handle)),
        "OpenProcessToken",
    )
    return handle


def create_low_integrity_token(base_token: wintypes.HANDLE) -> wintypes.HANDLE:
    """Derive the sandbox token: no privileges, Low integrity."""
    token = wintypes.HANDLE()
    ffi.check(
        ffi.CreateRestrictedToken(
            base_token, ffi.DISABLE_MAX_PRIVILEGE, 0, None, 0, None, 0, None, ctypes.byref(token)
        ),
        "CreateRestrictedToken",
    )
    with OwnedSid.from_string(ffi.LOW_INTEGRITY) as level:
        label = ffi.TOKEN_MANDATORY_LABEL()
        label.Label.Sid = level.pointer
        label.Label.Attributes = ffi.SE_GROUP_INTEGRITY
        ffi.check(
            ffi.SetTokenInformation(
                token, ffi.TOKEN_INTEGRITY_LEVEL_CLASS, ctypes.byref(label), ctypes.sizeof(label)
            ),
            "SetTokenInformation(TokenIntegrityLevel)",
        )
    _enable_privilege(token, CHANGE_NOTIFY_PRIVILEGE)
    return token


def _enable_privilege(token: wintypes.HANDLE, name: str) -> None:
    luid = ffi.LUID()
    ffi.check(ffi.LookupPrivilegeValueW(None, name, ctypes.byref(luid)), "LookupPrivilegeValueW")
    privileges = ffi.TOKEN_PRIVILEGES()
    privileges.PrivilegeCount = 1
    privileges.Privileges[0].Luid = luid
    privileges.Privileges[0].Attributes = ffi.SE_PRIVILEGE_ENABLED
    ctypes.set_last_error(0)
    ffi.check(
        ffi.AdjustTokenPrivileges(token, False, ctypes.byref(privileges), 0, None, None),
        "AdjustTokenPrivileges",
    )
    code = ctypes.get_last_error()
    if code != ffi.ERROR_SUCCESS:
        raise ffi.Win32Error("AdjustTokenPrivileges", code)


__all__ = ["CHANGE_NOTIFY_PRIVILEGE", "create_low_integrity_token", "open_current_process_token"]
