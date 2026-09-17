"""Raw Win32 bindings used by the Windows sandbox backend.

Only the declarations the sandbox needs, and nothing above them: every function
here maps one-to-one onto the API of the same name so the layers above read like
the Win32 documentation they were written from. Importing this module on a
non-Windows host raises immediately — there is no stub to accidentally run
against.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - guarded at import site
    raise ImportError("core.sandbox.oslayer.win32 只能在 Windows 上导入")

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ── Token access rights, creation flags and information classes ──────────────
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_ADJUST_DEFAULT = 0x0080

DISABLE_MAX_PRIVILEGE = 0x01

TOKEN_INTEGRITY_LEVEL_CLASS = 25
SE_GROUP_INTEGRITY = 0x00000020
SE_PRIVILEGE_ENABLED = 0x00000002

# ── Mandatory integrity labels (SDDL names, accepted by the SID and SD parsers)
LOW_INTEGRITY = "LW"
MEDIUM_INTEGRITY = "ME"
SDDL_REVISION_1 = 1
SE_FILE_OBJECT = 1
LABEL_SECURITY_INFORMATION = 0x00000010

ERROR_SUCCESS = 0
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3

# ── Process creation ─────────────────────────────────────────────────────────
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
STARTF_USESTDHANDLES = 0x00000100


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


OpenProcessToken = advapi32.OpenProcessToken
OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
OpenProcessToken.restype = wintypes.BOOL

SetTokenInformation = advapi32.SetTokenInformation
SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
SetTokenInformation.restype = wintypes.BOOL

CreateRestrictedToken = advapi32.CreateRestrictedToken
CreateRestrictedToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.HANDLE),
]
CreateRestrictedToken.restype = wintypes.BOOL

ConvertStringSidToSidW = advapi32.ConvertStringSidToSidW
ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
ConvertStringSidToSidW.restype = wintypes.BOOL

ConvertStringSecurityDescriptorToSecurityDescriptorW = (
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
)
ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.ULONG),
]
ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

ConvertSecurityDescriptorToStringSecurityDescriptorW = (
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
)
ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_wchar_p),
    ctypes.POINTER(wintypes.ULONG),
]
ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL

GetSecurityDescriptorSacl = advapi32.GetSecurityDescriptorSacl
GetSecurityDescriptorSacl.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.BOOL),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.BOOL),
]
GetSecurityDescriptorSacl.restype = wintypes.BOOL

LookupPrivilegeValueW = advapi32.LookupPrivilegeValueW
LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)]
LookupPrivilegeValueW.restype = wintypes.BOOL

AdjustTokenPrivileges = advapi32.AdjustTokenPrivileges
AdjustTokenPrivileges.argtypes = [
    wintypes.HANDLE,
    wintypes.BOOL,
    ctypes.POINTER(TOKEN_PRIVILEGES),
    wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
AdjustTokenPrivileges.restype = wintypes.BOOL

GetNamedSecurityInfoW = advapi32.GetNamedSecurityInfoW
GetNamedSecurityInfoW.argtypes = [
    wintypes.LPCWSTR,
    ctypes.c_int,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
]
GetNamedSecurityInfoW.restype = wintypes.DWORD

SetNamedSecurityInfoW = advapi32.SetNamedSecurityInfoW
SetNamedSecurityInfoW.argtypes = [
    wintypes.LPWSTR,
    ctypes.c_int,
    wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
SetNamedSecurityInfoW.restype = wintypes.DWORD

CreateProcessAsUserW = advapi32.CreateProcessAsUserW
CreateProcessAsUserW.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCWSTR,
    wintypes.LPWSTR,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.BOOL,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.LPCWSTR,
    ctypes.POINTER(STARTUPINFOW),
    ctypes.POINTER(PROCESS_INFORMATION),
]
CreateProcessAsUserW.restype = wintypes.BOOL

GetCurrentProcess = kernel32.GetCurrentProcess
GetCurrentProcess.argtypes = []
GetCurrentProcess.restype = wintypes.HANDLE

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL

LocalFree = kernel32.LocalFree
LocalFree.argtypes = [ctypes.c_void_p]
LocalFree.restype = ctypes.c_void_p

WaitForSingleObject = kernel32.WaitForSingleObject
WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
WaitForSingleObject.restype = wintypes.DWORD

GetExitCodeProcess = kernel32.GetExitCodeProcess
GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
GetExitCodeProcess.restype = wintypes.BOOL

TerminateProcess = kernel32.TerminateProcess
TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
TerminateProcess.restype = wintypes.BOOL


class Win32Error(OSError):
    """A Win32 call failed; carries the API name and ``GetLastError`` code."""

    def __init__(self, api: str, code: int) -> None:
        super().__init__(f"{api} 调用失败（Win32 错误码 {code}）")
        self.api = api
        self.code = code


def check(result: object, api: str) -> None:
    """Raise :class:`Win32Error` when a BOOL-returning API reported failure."""
    if not result:
        raise Win32Error(api, ctypes.get_last_error())


def check_status(status: int, api: str) -> None:
    """Raise :class:`Win32Error` when a status-returning API did not succeed."""
    if status != ERROR_SUCCESS:
        raise Win32Error(api, status)
