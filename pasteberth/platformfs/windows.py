"""Windows implementation of Pasteberth's semantic filesystem contract.

The module deliberately contains all Win32 details.  The rest of Pasteberth
only receives handles, identities, entries and semantic errors from here.
The supported Windows target is local NTFS; filesystems that cannot provide
the required handle and identity operations fail closed.
"""
from __future__ import annotations

import ctypes
import getpass
import hashlib
import ntpath
import os
from contextlib import contextmanager
from pathlib import Path

from pasteberth.platformfs.base import (
    BusyError,
    DirectoryHandle,
    EntryChangedError,
    EntryExistsError,
    EntryInfo,
    FileHandle,
    FileIdentity,
    PermissionAudit,
    PermissionSecurityError,
    PlatformCapabilities,
    PlatformFS,
    UnsafeLinkError,
    UnsupportedFilesystemError,
    VolumeSpace,
)


_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_HANDLE = 6
_ERROR_NOT_ENOUGH_MEMORY = 8
_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33
_ERROR_NOT_SUPPORTED = 50
_ERROR_INVALID_PARAMETER = 87
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_ALREADY_EXISTS = 183
_ERROR_FILE_EXISTS = 80
_ERROR_NOT_SAME_DEVICE = 17
_ERROR_CALL_NOT_IMPLEMENTED = 120
_ERROR_NO_MORE_FILES = 18
_ERROR_PRIVILEGE_NOT_HELD = 1314

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE = 0x00010000
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_WRITE_ATTRIBUTES = 0x00000100
_SYNCHRONIZE = 0x00100000
_FILE_LIST_DIRECTORY = 0x00000001

_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_FILE_SHARE_ALL = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE

_CREATE_NEW = 1
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4

_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

_FILE_ID_INFO_CLASS = 18
_FILE_BASIC_INFO_CLASS = 0
_FILE_DISPOSITION_INFO_CLASS = 4
_FILE_DISPOSITION_INFO_EX_CLASS = 21
_FILE_RENAME_INFO_CLASS = 3
_FILE_RENAME_INFO_EX_CLASS = 22

_FILE_RENAME_FLAG_REPLACE_IF_EXISTS = 0x00000001
_FILE_RENAME_FLAG_POSIX_SEMANTICS = 0x00000002
_FILE_DISPOSITION_FLAG_DELETE = 0x00000001
_FILE_DISPOSITION_FLAG_POSIX_SEMANTICS = 0x00000002

_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002

_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_TOKEN_QUERY = 0x0008
_TOKEN_USER_CLASS = 1
_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_ACCESS_ALLOWED_OBJECT_ACE_TYPE = 5

_FILE_GENERIC_READ = 0x120089
_FILE_GENERIC_WRITE = 0x120116
_FILE_GENERIC_EXECUTE = 0x1200A0
_FILE_DELETE_CHILD = 0x00000040
_GENERIC_ALL = 0x10000000


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


class _FILE_ID_128(ctypes.Structure):
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_uint64),
        ("FileId", _FILE_ID_128),
    ]


class _FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("FileAttributes", ctypes.c_uint32),
    ]


class _FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_int32)]


class _FILE_DISPOSITION_INFO_EX(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_uint32)]


class _FILE_RENAME_INFO(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_int32),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
        ("FileName", ctypes.c_wchar * 1),
    ]


class _FILE_RENAME_INFO_EX(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
        ("FileName", ctypes.c_wchar * 1),
    ]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", ctypes.c_uint32),
        ("OffsetHigh", ctypes.c_uint32),
        ("hEvent", ctypes.c_void_p),
    ]


class _WIN32_FIND_DATAW(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("dwReserved0", ctypes.c_uint32),
        ("dwReserved1", ctypes.c_uint32),
        ("cFileName", ctypes.c_wchar * 260),
        ("cAlternateFileName", ctypes.c_wchar * 14),
    ]


class _ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("AceCount", ctypes.c_uint32),
        ("AclBytesInUse", ctypes.c_uint32),
        ("AclBytesFree", ctypes.c_uint32),
    ]


class _ACE_HEADER(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_uint16),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", ctypes.c_uint32),
    ]


class _TOKEN_USER(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


class _WinApi:
    def __init__(self):
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        self.CreateFileW = self.kernel32.CreateFileW
        self.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self.CreateFileW.restype = ctypes.c_void_p
        self.CreateDirectoryW = self.kernel32.CreateDirectoryW
        self.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
        self.CreateDirectoryW.restype = ctypes.c_int
        self.CloseHandle = self.kernel32.CloseHandle
        self.CloseHandle.argtypes = [ctypes.c_void_p]
        self.CloseHandle.restype = ctypes.c_int
        self.DeleteFileW = self.kernel32.DeleteFileW
        self.DeleteFileW.argtypes = [ctypes.c_wchar_p]
        self.DeleteFileW.restype = ctypes.c_int
        self.GetFileInformationByHandle = self.kernel32.GetFileInformationByHandle
        self.GetFileInformationByHandle.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
        ]
        self.GetFileInformationByHandle.restype = ctypes.c_int
        self.GetFileInformationByHandleEx = self.kernel32.GetFileInformationByHandleEx
        self.GetFileInformationByHandleEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self.GetFileInformationByHandleEx.restype = ctypes.c_int
        self.SetFileInformationByHandle = self.kernel32.SetFileInformationByHandle
        self.SetFileInformationByHandle.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self.SetFileInformationByHandle.restype = ctypes.c_int
        self.GetFinalPathNameByHandleW = self.kernel32.GetFinalPathNameByHandleW
        self.GetFinalPathNameByHandleW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
        self.CreateHardLinkW = self.kernel32.CreateHardLinkW
        self.CreateHardLinkW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_void_p,
        ]
        self.CreateHardLinkW.restype = ctypes.c_int
        self.FlushFileBuffers = self.kernel32.FlushFileBuffers
        self.FlushFileBuffers.argtypes = [ctypes.c_void_p]
        self.FlushFileBuffers.restype = ctypes.c_int
        self.GetDiskFreeSpaceExW = self.kernel32.GetDiskFreeSpaceExW
        self.GetDiskFreeSpaceExW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        ]
        self.GetDiskFreeSpaceExW.restype = ctypes.c_int
        self.FindFirstFileW = self.kernel32.FindFirstFileW
        self.FindFirstFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(_WIN32_FIND_DATAW),
        ]
        self.FindFirstFileW.restype = ctypes.c_void_p
        self.FindNextFileW = self.kernel32.FindNextFileW
        self.FindNextFileW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_WIN32_FIND_DATAW),
        ]
        self.FindNextFileW.restype = ctypes.c_int
        self.FindClose = self.kernel32.FindClose
        self.FindClose.argtypes = [ctypes.c_void_p]
        self.FindClose.restype = ctypes.c_int
        self.LockFileEx = self.kernel32.LockFileEx
        self.LockFileEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(_OVERLAPPED),
        ]
        self.LockFileEx.restype = ctypes.c_int
        self.UnlockFileEx = self.kernel32.UnlockFileEx
        self.UnlockFileEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(_OVERLAPPED),
        ]
        self.UnlockFileEx.restype = ctypes.c_int
        self.GetCurrentProcess = self.kernel32.GetCurrentProcess
        self.GetCurrentProcess.argtypes = []
        self.GetCurrentProcess.restype = ctypes.c_void_p
        self.OpenProcessToken = self.advapi32.OpenProcessToken
        self.OpenProcessToken.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.OpenProcessToken.restype = ctypes.c_int
        self.GetTokenInformation = self.advapi32.GetTokenInformation
        self.GetTokenInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.GetTokenInformation.restype = ctypes.c_int
        self.GetSecurityInfo = self.advapi32.GetSecurityInfo
        self.GetSecurityInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.GetSecurityInfo.restype = ctypes.c_uint32
        self.GetSecurityDescriptorDacl = self.advapi32.GetSecurityDescriptorDacl
        self.GetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_int),
        ]
        self.GetSecurityDescriptorDacl.restype = ctypes.c_int
        self.GetAclInformation = self.advapi32.GetAclInformation
        self.GetAclInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        self.GetAclInformation.restype = ctypes.c_int
        self.GetAce = self.advapi32.GetAce
        self.GetAce.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.GetAce.restype = ctypes.c_int
        self.ConvertSidToStringSidW = self.advapi32.ConvertSidToStringSidW
        self.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        self.ConvertSidToStringSidW.restype = ctypes.c_int
        self.ConvertStringSecurityDescriptorToSecurityDescriptorW = (
            self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        )
        self.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
        self.SetSecurityInfo = self.advapi32.SetSecurityInfo
        self.SetSecurityInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.SetSecurityInfo.restype = ctypes.c_uint32
        self.LocalFree = self.kernel32.LocalFree
        self.LocalFree.argtypes = [ctypes.c_void_p]
        self.LocalFree.restype = ctypes.c_void_p


class _WindowsDirectoryHandle(DirectoryHandle):
    def __init__(self, api: _WinApi, path: Path, handle: int, identity: FileIdentity):
        super().__init__(path, identity)
        self._api = api
        self._handle = handle

    def _close_native(self) -> None:
        self._api.CloseHandle(ctypes.c_void_p(self._handle))


class _WindowsFileHandle(FileHandle):
    def __init__(self, api: _WinApi, stream, handle: int, identity: FileIdentity, size: int):
        super().__init__(stream, identity, size)
        self._api = api
        self._handle = handle

    def _sync_native(self) -> None:
        if not self._api.FlushFileBuffers(ctypes.c_void_p(self._handle)):
            _raise_code(ctypes.get_last_error(), "FlushFileBuffers")

    def close(self) -> None:
        if not self._closed:
            try:
                super().close()
            finally:
                self._handle = 0


def _handle_value(handle) -> int:
    if isinstance(handle, ctypes.c_void_p):
        return int(handle.value or 0)
    return int(handle or 0)


def _valid_handle(handle) -> bool:
    value = _handle_value(handle)
    return value not in (0, _INVALID_HANDLE_VALUE)


def _raise_code(code: int, operation: str, path: str | None = None):
    message = ctypes.FormatError(code)
    if code in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
        raise FileNotFoundError(code, message, path or operation)
    if code == _ERROR_ALREADY_EXISTS or code == _ERROR_FILE_EXISTS:
        raise EntryExistsError(code, message, path or operation)
    if code in (_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION):
        raise BusyError(code, message, path or operation)
    if code == _ERROR_ACCESS_DENIED:
        raise PermissionError(code, message, path or operation)
    if code in (
        _ERROR_INVALID_PARAMETER,
        _ERROR_NOT_SUPPORTED,
        _ERROR_CALL_NOT_IMPLEMENTED,
        _ERROR_NOT_SAME_DEVICE,
    ):
        raise UnsupportedFilesystemError(
            f"{operation} indisponible ({code}: {message})"
        )
    raise OSError(code, message, path or operation)


def _filetime_value(value: _FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


class WindowsPlatformFS(PlatformFS):
    backend_name = "windows"

    def __init__(self):
        self._api = _WinApi()
        self._user_sid: str | None = None

    @property
    def capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            backend=self.backend_name,
            safe_directory_open=True,
            safe_file_open=True,
            exclusive_create=True,
            identity=True,
            hard_link_guard=True,
            atomic_no_replace_rename=True,
            expected_remove=True,
            interprocess_locks=True,
            file_flush=True,
            directory_flush=True,
            volume_space=True,
            volume_identity=True,
            private_permissions=True,
        )

    @staticmethod
    def _path_string(path: Path | str) -> str:
        value = os.fspath(path)
        if isinstance(value, bytes):
            value = os.fsdecode(value)
        value = value.replace("/", "\\")
        if not ntpath.isabs(value):
            raise ValueError(f"chemin absolu attendu : {value!r}")
        return ntpath.normpath(value)

    @staticmethod
    def _extended_path(value: str) -> str:
        if value.startswith(("\\\\?\\", "\\\\.\\")):
            return value
        drive, tail = ntpath.splitdrive(value)
        if drive.startswith("\\\\"):
            return "\\\\?\\UNC" + drive[1:] + tail
        if drive:
            return "\\\\?\\" + drive + tail
        return value

    @staticmethod
    def _compare_path(value: str) -> str:
        value = value.replace("/", "\\")
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        value = ntpath.normpath(value)
        if len(value) > 3:
            value = value.rstrip("\\")
        return ntpath.normcase(value)

    @classmethod
    def _path_components(cls, path: Path | str) -> tuple[str, list[str]]:
        value = cls._path_string(path)
        drive, tail = ntpath.splitdrive(value)
        root = drive + "\\"
        parts = [part for part in tail.split("\\") if part and part != "."]
        return root, parts

    def _open_native(
        self,
        path: str,
        *,
        desired_access: int,
        disposition: int = _OPEN_EXISTING,
    ) -> int:
        flags = _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS
        handle = self._api.CreateFileW(
            self._extended_path(path),
            desired_access,
            _FILE_SHARE_ALL,
            None,
            disposition,
            flags,
            None,
        )
        if not _valid_handle(handle):
            _raise_code(ctypes.get_last_error(), "CreateFileW", path)
        return _handle_value(handle)

    def _close_native(self, handle: int) -> None:
        if handle:
            self._api.CloseHandle(ctypes.c_void_p(handle))

    def _final_path(self, handle: int) -> str:
        size = 512
        while size <= 32768:
            buffer = ctypes.create_unicode_buffer(size)
            result = self._api.GetFinalPathNameByHandleW(
                ctypes.c_void_p(handle),
                buffer,
                size,
                0,
            )
            if result == 0:
                _raise_code(ctypes.get_last_error(), "GetFinalPathNameByHandleW")
            if result < size - 1:
                return buffer.value
            size *= 2
        raise UnsupportedFilesystemError("chemin Windows trop long à vérifier")

    def _query_raw(self, handle: int) -> dict:
        basic = _BY_HANDLE_FILE_INFORMATION()
        if not self._api.GetFileInformationByHandle(
            ctypes.c_void_p(handle),
            ctypes.byref(basic),
        ):
            _raise_code(ctypes.get_last_error(), "GetFileInformationByHandle")
        file_id = _FILE_ID_INFO()
        if not self._api.GetFileInformationByHandleEx(
            ctypes.c_void_p(handle),
            _FILE_ID_INFO_CLASS,
            ctypes.byref(file_id),
            ctypes.sizeof(file_id),
        ):
            _raise_code(ctypes.get_last_error(), "FileIdInfo")
        basic_info = _FILE_BASIC_INFO()
        if not self._api.GetFileInformationByHandleEx(
            ctypes.c_void_p(handle),
            _FILE_BASIC_INFO_CLASS,
            ctypes.byref(basic_info),
            ctypes.sizeof(basic_info),
        ):
            _raise_code(ctypes.get_last_error(), "FileBasicInfo")
        identifier = int.from_bytes(bytes(file_id.FileId.Identifier), "little")
        return {
            "identity": FileIdentity(int(file_id.VolumeSerialNumber), identifier),
            "attributes": int(basic.dwFileAttributes),
            "size": (int(basic.nFileSizeHigh) << 32) | int(basic.nFileSizeLow),
            "modified_ns": int(basic_info.LastWriteTime),
            "changed_ns": int(basic_info.ChangeTime),
        }

    @staticmethod
    def _entry_from_raw(name: str, raw: dict, owner: str | None = None) -> EntryInfo:
        attributes = raw["attributes"]
        is_reparse = bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
        is_directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
        return EntryInfo(
            name=name,
            identity=raw["identity"],
            size=raw["size"],
            is_regular=not is_directory and not is_reparse,
            is_symlink=is_reparse,
            owner=owner,
            mode=None,
            modified_ns=raw["modified_ns"],
            changed_ns=raw["changed_ns"],
        )

    def _sid_string(self, sid: int | ctypes.c_void_p) -> str:
        output = ctypes.c_wchar_p()
        if not self._api.ConvertSidToStringSidW(
            ctypes.c_void_p(_handle_value(sid)),
            ctypes.byref(output),
        ):
            _raise_code(ctypes.get_last_error(), "ConvertSidToStringSidW")
        try:
            return output.value or ""
        finally:
            self._api.LocalFree(ctypes.cast(output, ctypes.c_void_p))

    def _owner_sid(self, handle: int) -> str:
        owner = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = self._api.GetSecurityInfo(
            ctypes.c_void_p(handle),
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise PermissionSecurityError(
                f"lecture du propriétaire Windows impossible ({result})"
            )
        try:
            if not owner.value:
                raise PermissionSecurityError("propriétaire Windows absent")
            return self._sid_string(owner)
        finally:
            if descriptor.value:
                self._api.LocalFree(descriptor)

    def _current_user_sid_value(self) -> str:
        if self._user_sid is not None:
            return self._user_sid
        token = ctypes.c_void_p()
        if not self._api.OpenProcessToken(
            self._api.GetCurrentProcess(),
            _TOKEN_QUERY,
            ctypes.byref(token),
        ):
            _raise_code(ctypes.get_last_error(), "OpenProcessToken")
        try:
            size = ctypes.c_uint32()
            self._api.GetTokenInformation(
                token,
                _TOKEN_USER_CLASS,
                None,
                0,
                ctypes.byref(size),
            )
            if not size.value:
                _raise_code(ctypes.get_last_error(), "GetTokenInformation")
            buffer = ctypes.create_string_buffer(size.value)
            if not self._api.GetTokenInformation(
                token,
                _TOKEN_USER_CLASS,
                buffer,
                size.value,
                ctypes.byref(size),
            ):
                _raise_code(ctypes.get_last_error(), "GetTokenInformation")
            user = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER)).contents
            self._user_sid = self._sid_string(user.User.Sid)
            return self._user_sid
        finally:
            self._api.CloseHandle(token)

    def _owner_for_entry(self, handle: int) -> str | None:
        try:
            return self._owner_sid(handle)
        except (OSError, PermissionSecurityError, UnsupportedFilesystemError):
            return None

    def _security_audit_handle(self, handle: int) -> tuple[str, bool, str | None]:
        owner = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        result = self._api.GetSecurityInfo(
            ctypes.c_void_p(handle),
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise PermissionSecurityError(
                f"lecture de l'ACL Windows impossible ({result})"
            )
        try:
            if not owner.value:
                return "", False, "propriétaire absent"
            owner_sid = self._sid_string(owner)
            present = ctypes.c_int()
            defaulted = ctypes.c_int()
            dacl_pointer = ctypes.c_void_p()
            if not self._api.GetSecurityDescriptorDacl(
                descriptor,
                ctypes.byref(present),
                ctypes.byref(dacl_pointer),
                ctypes.byref(defaulted),
            ):
                _raise_code(ctypes.get_last_error(), "GetSecurityDescriptorDacl")
            if not present.value or not dacl_pointer.value:
                return owner_sid, False, "DACL absente ou héritée implicitement"
            size_info = _ACL_SIZE_INFORMATION()
            if not self._api.GetAclInformation(
                dacl_pointer,
                ctypes.byref(size_info),
                ctypes.sizeof(size_info),
                _ACL_SIZE_INFORMATION_CLASS,
            ):
                _raise_code(ctypes.get_last_error(), "GetAclInformation")
            sensitive = (
                _FILE_GENERIC_READ
                | _FILE_GENERIC_WRITE
                | _FILE_GENERIC_EXECUTE
                | _FILE_DELETE_CHILD
                | _GENERIC_ALL
            )
            for index in range(size_info.AceCount):
                ace = ctypes.c_void_p()
                if not self._api.GetAce(
                    dacl_pointer,
                    index,
                    ctypes.byref(ace),
                ):
                    _raise_code(ctypes.get_last_error(), "GetAce")
                header = ctypes.cast(ace, ctypes.POINTER(_ACE_HEADER)).contents
                if header.AceType not in (
                    _ACCESS_ALLOWED_ACE_TYPE,
                    _ACCESS_ALLOWED_OBJECT_ACE_TYPE,
                ):
                    continue
                mask = ctypes.c_uint32.from_address(_handle_value(ace) + 4).value
                sid_address = _handle_value(ace) + 8
                ace_sid = self._sid_string(sid_address)
                if ace_sid != owner_sid and mask & sensitive:
                    return owner_sid, False, "ACL autorisant un autre principal"
            return owner_sid, True, None
        finally:
            if descriptor.value:
                self._api.LocalFree(descriptor)

    def _set_private_acl(self, path: str) -> None:
        descriptor = ctypes.c_void_p()
        # Use the concrete process SID rather than the Owner Rights alias so
        # the resulting DACL can be audited consistently on every runtime.
        sddl = f"D:P(A;;FA;;;{self._current_user_sid_value()})"
        if not self._api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            1,
            ctypes.byref(descriptor),
            None,
        ):
            _raise_code(ctypes.get_last_error(), "ConvertStringSecurityDescriptor")
        try:
            present = ctypes.c_int()
            dacl = ctypes.c_void_p()
            defaulted = ctypes.c_int()
            if not self._api.GetSecurityDescriptorDacl(
                descriptor,
                ctypes.byref(present),
                ctypes.byref(dacl),
                ctypes.byref(defaulted),
            ):
                _raise_code(ctypes.get_last_error(), "GetSecurityDescriptorDacl")
            handle = self._open_native(
                path,
                desired_access=_READ_CONTROL | _WRITE_DAC | _SYNCHRONIZE,
            )
            try:
                result = self._api.SetSecurityInfo(
                    ctypes.c_void_p(handle),
                    _SE_FILE_OBJECT,
                    _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    dacl,
                    None,
                )
            finally:
                self._close_native(handle)
            if result:
                raise PermissionSecurityError(
                    f"ACL privée impossible pour {path} ({result})"
                )
        finally:
            if descriptor.value:
                self._api.LocalFree(descriptor)

    def _directory_stable(self, directory: DirectoryHandle) -> None:
        if not isinstance(directory, _WindowsDirectoryHandle):
            raise TypeError("handle Windows attendu")
        current = self._open_native(
            self._path_string(directory.path),
            desired_access=_GENERIC_READ | _READ_CONTROL | _SYNCHRONIZE,
        )
        try:
            raw = self._query_raw(current)
            if raw["identity"] != directory.identity:
                raise EntryChangedError(f"répertoire remplacé : {directory.path}")
            if self._compare_path(self._final_path(current)) != self._compare_path(
                self._path_string(directory.path)
            ):
                raise UnsafeLinkError(f"chemin de zone redirigé : {directory.path}")
        finally:
            self._close_native(current)

    def open_directory(
        self,
        path: Path,
        *,
        create: bool = False,
        mode: int = 0o700,
    ) -> _WindowsDirectoryHandle:
        root, parts = self._path_components(path)
        current = root
        handle = self._open_native(
            current,
            desired_access=_GENERIC_READ | _READ_CONTROL | _SYNCHRONIZE,
        )
        try:
            raw = self._query_raw(handle)
            if raw["attributes"] & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise UnsafeLinkError(f"répertoire symbolique refusé : {current}")
            if not raw["attributes"] & _FILE_ATTRIBUTE_DIRECTORY:
                raise NotADirectoryError(current)
            for part in parts:
                next_path = ntpath.join(current, part)
                try:
                    next_handle = self._open_native(
                        next_path,
                        desired_access=_GENERIC_READ | _READ_CONTROL | _SYNCHRONIZE,
                    )
                    created = False
                except FileNotFoundError:
                    if not create:
                        raise
                    if not self._api.CreateDirectoryW(self._extended_path(next_path), None):
                        code = ctypes.get_last_error()
                        if code != _ERROR_ALREADY_EXISTS:
                            _raise_code(code, "CreateDirectoryW", next_path)
                    next_handle = self._open_native(
                        next_path,
                        desired_access=_GENERIC_READ | _READ_CONTROL | _SYNCHRONIZE,
                    )
                    created = True
                try:
                    next_raw = self._query_raw(next_handle)
                    if next_raw["attributes"] & _FILE_ATTRIBUTE_REPARSE_POINT:
                        raise UnsafeLinkError(f"répertoire symbolique refusé : {next_path}")
                    if not next_raw["attributes"] & _FILE_ATTRIBUTE_DIRECTORY:
                        raise NotADirectoryError(next_path)
                    if self._compare_path(self._final_path(next_handle)) != self._compare_path(
                        next_path
                    ):
                        raise UnsafeLinkError(f"chemin de zone redirigé : {next_path}")
                    if created:
                        self._set_private_acl(next_path)
                except BaseException:
                    self._close_native(next_handle)
                    raise
                self._close_native(handle)
                handle = next_handle
                current = next_path
            final_raw = self._query_raw(handle)
            if self._compare_path(self._final_path(handle)) != self._compare_path(
                self._path_string(path)
            ):
                raise UnsafeLinkError(f"chemin de zone redirigé : {path}")
            return _WindowsDirectoryHandle(
                self._api,
                Path(path),
                handle,
                final_raw["identity"],
            )
        except BaseException:
            self._close_native(handle)
            raise

    def first_symlink_component(self, path: Path) -> Path | None:
        root, parts = self._path_components(path)
        current = root
        handle = None
        try:
            try:
                handle = self._open_native(
                    current,
                    desired_access=_GENERIC_READ | _READ_CONTROL | _SYNCHRONIZE,
                )
            except FileNotFoundError:
                return None
            for part in parts:
                next_path = ntpath.join(current, part)
                try:
                    next_handle = self._open_native(
                        next_path,
                        desired_access=_GENERIC_READ | _READ_CONTROL | _SYNCHRONIZE,
                    )
                except FileNotFoundError:
                    return None
                raw = self._query_raw(next_handle)
                self._close_native(handle)
                handle = next_handle
                current = next_path
                if raw["attributes"] & _FILE_ATTRIBUTE_REPARSE_POINT:
                    return Path(current)
                if not raw["attributes"] & _FILE_ATTRIBUTE_DIRECTORY:
                    return None
            return None
        finally:
            self._close_native(handle)

    def _entry_native(
        self,
        directory: DirectoryHandle,
        name: str,
        *,
        desired_access: int,
        require_regular: bool,
    ) -> tuple[int, dict]:
        self.validate_component(name)
        self._directory_stable(directory)
        directory_path = self._path_string(directory.path)
        path = ntpath.join(directory_path, name)
        handle = self._open_native(path, desired_access=desired_access)
        try:
            raw = self._query_raw(handle)
            if self._compare_path(self._final_path(handle)) != self._compare_path(path):
                raise UnsafeLinkError(f"chemin d'entrée redirigé : {name!r}")
            if require_regular and (
                raw["attributes"] & _FILE_ATTRIBUTE_REPARSE_POINT
                or raw["attributes"] & _FILE_ATTRIBUTE_DIRECTORY
            ):
                raise UnsafeLinkError(f"fichier non régulier : {name!r}")
            self._directory_stable(directory)
            return handle, raw
        except BaseException:
            self._close_native(handle)
            raise

    def _stream_from_handle(self, handle: int, mode: str):
        import msvcrt

        if mode in {"rb", "r"}:
            flags = os.O_RDONLY
        elif mode in {"wb", "w"}:
            flags = os.O_WRONLY
        else:
            flags = os.O_RDWR
        flags |= getattr(os, "O_BINARY", 0)
        fd = msvcrt.open_osfhandle(handle, flags)
        try:
            if "b" in mode:
                return os.fdopen(fd, mode)
            return os.fdopen(fd, mode, encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise

    def open_existing(
        self,
        directory: DirectoryHandle,
        name: str,
        *,
        mode: str = "rb",
    ) -> _WindowsFileHandle:
        if mode in {"rb", "r"}:
            desired = _GENERIC_READ | _READ_CONTROL | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
        else:
            desired = _GENERIC_READ | _GENERIC_WRITE | _READ_CONTROL | _SYNCHRONIZE
        handle, raw = self._entry_native(
            directory,
            name,
            desired_access=desired,
            require_regular=True,
        )
        try:
            native_handle = handle
            stream = self._stream_from_handle(handle, mode)
            handle = 0
            return _WindowsFileHandle(
                self._api,
                stream,
                native_handle,
                raw["identity"],
                raw["size"],
            )
        except BaseException:
            if handle:
                self._close_native(handle)
            raise

    def create_exclusive(
        self,
        directory: DirectoryHandle,
        name: str,
        *,
        mode: str = "wb",
        permissions: int = 0o600,
    ) -> _WindowsFileHandle:
        self.validate_component(name)
        self._directory_stable(directory)
        path = ntpath.join(self._path_string(directory.path), name)
        handle = self._open_native(
            path,
            desired_access=(
                _GENERIC_READ
                | _GENERIC_WRITE
                | _READ_CONTROL
                | _FILE_WRITE_ATTRIBUTES
                | _SYNCHRONIZE
            ),
            disposition=_CREATE_NEW,
        )
        try:
            raw = self._query_raw(handle)
            if raw["attributes"] & (
                _FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY
            ):
                raise UnsafeLinkError(f"fichier non régulier : {name!r}")
            self._set_private_acl(path)
            native_handle = handle
            stream = self._stream_from_handle(handle, mode)
            handle = 0
            return _WindowsFileHandle(
                self._api,
                stream,
                native_handle,
                raw["identity"],
                raw["size"],
            )
        except BaseException:
            if handle:
                self._close_native(handle)
            raise

    def entry_info(self, directory: DirectoryHandle, name: str) -> EntryInfo | None:
        try:
            handle, raw = self._entry_native(
                directory,
                name,
                desired_access=_GENERIC_READ | _READ_CONTROL | _FILE_READ_ATTRIBUTES,
                require_regular=False,
            )
        except FileNotFoundError:
            return None
        try:
            return self._entry_from_raw(name, raw, self._owner_for_entry(handle))
        finally:
            self._close_native(handle)

    def entries(self, directory: DirectoryHandle) -> tuple[EntryInfo, ...]:
        self._directory_stable(directory)
        pattern = ntpath.join(self._path_string(directory.path), "*")
        data = _WIN32_FIND_DATAW()
        search = self._api.FindFirstFileW(self._extended_path(pattern), ctypes.byref(data))
        if not _valid_handle(search):
            _raise_code(ctypes.get_last_error(), "FindFirstFileW", pattern)
        search_handle = _handle_value(search)
        result: list[EntryInfo] = []
        try:
            while True:
                name = data.cFileName
                if name not in (".", ".."):
                    try:
                        entry = self.entry_info(directory, name)
                    except FileNotFoundError:
                        entry = None
                    if entry is not None:
                        result.append(entry)
                if self._api.FindNextFileW(search_handle, ctypes.byref(data)):
                    continue
                code = ctypes.get_last_error()
                if code == _ERROR_NO_MORE_FILES:
                    break
                _raise_code(code, "FindNextFileW", pattern)
        finally:
            self._api.FindClose(ctypes.c_void_p(search_handle))
        return tuple(result)

    def identity(
        self,
        directory: DirectoryHandle,
        name: str,
        *,
        require_regular: bool = True,
    ) -> FileIdentity | None:
        info = self.entry_info(directory, name)
        if info is None:
            return None
        if require_regular and not info.is_regular:
            raise UnsafeLinkError(f"fichier non régulier : {name!r}")
        return info.identity

    def is_owned(self, entry: EntryInfo) -> bool:
        if entry.owner is None:
            return False
        try:
            return entry.owner == self._current_user_sid_value()
        except (OSError, PermissionSecurityError, UnsupportedFilesystemError):
            return False

    def _check_expected(
        self,
        directory: DirectoryHandle,
        name: str,
        expected: FileIdentity,
    ) -> EntryInfo:
        info = self.entry_info(directory, name)
        if info is None or info.identity != expected:
            raise EntryChangedError(f"fichier modifié : {name!r}")
        if not info.is_regular or not self.is_owned(info):
            raise PermissionSecurityError(f"fichier non détenu : {name!r}")
        return info

    def link_expected(
        self,
        directory: DirectoryHandle,
        source: str,
        target: str,
        expected: FileIdentity,
    ) -> None:
        self._check_expected(directory, source, expected)
        self.validate_component(target)
        source_path = ntpath.join(self._path_string(directory.path), source)
        target_path = ntpath.join(self._path_string(directory.path), target)
        self._directory_stable(directory)
        if not self._api.CreateHardLinkW(
            self._extended_path(target_path),
            self._extended_path(source_path),
            None,
        ):
            _raise_code(ctypes.get_last_error(), "CreateHardLinkW", target_path)
        self._directory_stable(directory)
        if self.identity(directory, target) != expected:
            raise EntryChangedError(f"cible étrangère apparue : {target!r}")

    def _rename_buffer(self, target: str, directory: DirectoryHandle, *, replace: bool):
        target_path = self._extended_path(
            ntpath.join(self._path_string(directory.path), target)
        )
        target_bytes = target_path.encode("utf-16-le")
        if replace:
            info_type = _FILE_RENAME_INFO_EX
            size = info_type.FileName.offset + len(target_bytes)
            buffer = ctypes.create_string_buffer(size + ctypes.sizeof(ctypes.c_wchar))
            info = ctypes.cast(buffer, ctypes.POINTER(info_type)).contents
            info.Flags = _FILE_RENAME_FLAG_REPLACE_IF_EXISTS | _FILE_RENAME_FLAG_POSIX_SEMANTICS
        else:
            info_type = _FILE_RENAME_INFO_EX
            size = info_type.FileName.offset + len(target_bytes)
            buffer = ctypes.create_string_buffer(size + ctypes.sizeof(ctypes.c_wchar))
            info = ctypes.cast(buffer, ctypes.POINTER(info_type)).contents
            info.Flags = 0
        # Use a fully qualified path with a null root.  This avoids a second
        # relative target-directory open and works with older Win32 runtimes.
        info.RootDirectory = None
        info.FileNameLength = len(target_bytes)
        ctypes.memmove(
            ctypes.addressof(buffer) + info_type.FileName.offset,
            target_bytes,
            len(target_bytes),
        )
        return buffer, size

    def _rename_buffer_legacy(self, target: str, directory: DirectoryHandle, *, replace: bool):
        target_path = self._extended_path(
            ntpath.join(self._path_string(directory.path), target)
        )
        target_bytes = target_path.encode("utf-16-le")
        info_type = _FILE_RENAME_INFO
        size = info_type.FileName.offset + len(target_bytes)
        buffer = ctypes.create_string_buffer(size + ctypes.sizeof(ctypes.c_wchar))
        info = ctypes.cast(buffer, ctypes.POINTER(info_type)).contents
        info.ReplaceIfExists = int(replace)
        info.RootDirectory = None
        info.FileNameLength = len(target_bytes)
        ctypes.memmove(
            ctypes.addressof(buffer) + info_type.FileName.offset,
            target_bytes,
            len(target_bytes),
        )
        return buffer, size

    def _rename_native(
        self,
        handle: int,
        directory: DirectoryHandle,
        target: str,
        *,
        replace: bool,
    ) -> None:
        buffer, size = self._rename_buffer(target, directory, replace=replace)
        if self._api.SetFileInformationByHandle(
            ctypes.c_void_p(handle),
            _FILE_RENAME_INFO_EX_CLASS,
            ctypes.byref(buffer),
            size,
        ):
            return
        code = ctypes.get_last_error()
        if code not in (_ERROR_INVALID_PARAMETER, _ERROR_NOT_SUPPORTED, _ERROR_CALL_NOT_IMPLEMENTED):
            _raise_code(code, "FileRenameInfoEx", target)
        buffer, size = self._rename_buffer_legacy(target, directory, replace=replace)
        if not self._api.SetFileInformationByHandle(
            ctypes.c_void_p(handle),
            _FILE_RENAME_INFO_CLASS,
            ctypes.byref(buffer),
            size,
        ):
            _raise_code(ctypes.get_last_error(), "FileRenameInfo", target)

    def _rename(
        self,
        directory: DirectoryHandle,
        source: str,
        target: str,
        *,
        replace: bool,
        expected_source: FileIdentity | None,
        expected_target: FileIdentity | None,
    ) -> None:
        self.validate_component(source)
        self.validate_component(target)
        handle, raw = self._entry_native(
            directory,
            source,
            desired_access=_GENERIC_READ | _READ_CONTROL | _DELETE | _SYNCHRONIZE,
            require_regular=True,
        )
        try:
            source_identity = raw["identity"]
            if expected_source is not None and source_identity != expected_source:
                raise EntryChangedError(f"source modifiée : {source!r}")
            # The directory and identity checks above mirror the POSIX
            # contract.  Do not query the source ACL before renaming: some
            # Windows-compatible runtimes reject a later rename after a
            # security-descriptor query on the open source handle.
            target_info = self.entry_info(directory, target)
            if not replace:
                if target_info is not None:
                    raise EntryExistsError(f"cible déjà existante : {target!r}")
            elif target_info is not None:
                if not target_info.is_regular or not self.is_owned(target_info):
                    raise PermissionSecurityError(f"cible non remplaçable : {target!r}")
                if expected_target is not None and target_info.identity != expected_target:
                    raise EntryChangedError(f"cible modifiée : {target!r}")
            elif expected_target is not None:
                raise EntryChangedError(f"cible disparue : {target!r}")
            self._rename_native(handle, directory, target, replace=replace)
            self._directory_stable(directory)
            if self.identity(directory, target) != source_identity:
                raise EntryChangedError(f"cible étrangère apparue : {target!r}")
        finally:
            self._close_native(handle)

    def rename_noreplace(
        self,
        directory: DirectoryHandle,
        source: str,
        target: str,
        *,
        expected: FileIdentity | None = None,
    ) -> None:
        self._rename(
            directory,
            source,
            target,
            replace=False,
            expected_source=expected,
            expected_target=None,
        )

    def replace(
        self,
        directory: DirectoryHandle,
        source: str,
        target: str,
        *,
        expected_source: FileIdentity | None = None,
        expected_target: FileIdentity | None = None,
    ) -> None:
        self._rename(
            directory,
            source,
            target,
            replace=True,
            expected_source=expected_source,
            expected_target=expected_target,
        )

    def remove_expected(
        self,
        directory: DirectoryHandle,
        name: str,
        expected: FileIdentity | None,
    ) -> bool:
        if expected is None:
            return False
        try:
            handle, raw = self._entry_native(
                directory,
                name,
                desired_access=_GENERIC_READ | _READ_CONTROL | _DELETE | _SYNCHRONIZE,
                require_regular=True,
            )
        except FileNotFoundError:
            return True
        try:
            if raw["identity"] != expected:
                return False
            info = self.entry_info(directory, name)
            if info is None or info.identity != raw["identity"]:
                return False
            if not self.is_owned(info):
                return False
            disposition = _FILE_DISPOSITION_INFO_EX(
                _FILE_DISPOSITION_FLAG_DELETE | _FILE_DISPOSITION_FLAG_POSIX_SEMANTICS
            )
            if not self._api.SetFileInformationByHandle(
                ctypes.c_void_p(handle),
                _FILE_DISPOSITION_INFO_EX_CLASS,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            ):
                code = ctypes.get_last_error()
                if code not in (
                    _ERROR_INVALID_PARAMETER,
                    _ERROR_NOT_SUPPORTED,
                    _ERROR_CALL_NOT_IMPLEMENTED,
                ):
                    _raise_code(code, "FileDispositionInfoEx", name)
                legacy = _FILE_DISPOSITION_INFO(1)
                if not self._api.SetFileInformationByHandle(
                    ctypes.c_void_p(handle),
                    _FILE_DISPOSITION_INFO_CLASS,
                    ctypes.byref(legacy),
                    ctypes.sizeof(legacy),
                ):
                    _raise_code(ctypes.get_last_error(), "FileDispositionInfo", name)
            return True
        finally:
            self._close_native(handle)

    @contextmanager
    def acquire_lock(
        self,
        directory: DirectoryHandle,
        *,
        name: str = ".pasteberth.lock",
        exclusive: bool,
        blocking: bool = True,
    ):
        self.validate_component(name)
        self._directory_stable(directory)
        path = ntpath.join(self._path_string(directory.path), name)
        handle = self._open_native(
            path,
            desired_access=_GENERIC_READ | _GENERIC_WRITE | _READ_CONTROL | _SYNCHRONIZE,
            disposition=_OPEN_ALWAYS,
        )
        overlapped = _OVERLAPPED()
        locked = False
        try:
            raw = self._query_raw(handle)
            if raw["attributes"] & (
                _FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DIRECTORY
            ):
                raise UnsafeLinkError(f"verrou non régulier : {name!r}")
            info = self.entry_info(directory, name)
            if info is None or info.identity != raw["identity"]:
                raise EntryChangedError(f"verrou modifié : {name!r}")
            if not self.is_owned(info):
                raise PermissionSecurityError(f"verrou non détenu : {name!r}")
            flags = _LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0
            if not blocking:
                flags |= _LOCKFILE_FAIL_IMMEDIATELY
            if not self._api.LockFileEx(
                ctypes.c_void_p(handle),
                flags,
                0,
                0xFFFFFFFF,
                0xFFFFFFFF,
                ctypes.byref(overlapped),
            ):
                code = ctypes.get_last_error()
                if code in (_ERROR_LOCK_VIOLATION, _ERROR_SHARING_VIOLATION):
                    raise BusyError(code, ctypes.FormatError(code), name)
                _raise_code(code, "LockFileEx", name)
            locked = True
            yield
        finally:
            if locked:
                self._api.UnlockFileEx(
                    ctypes.c_void_p(handle),
                    0,
                    0xFFFFFFFF,
                    0xFFFFFFFF,
                    ctypes.byref(overlapped),
                )
            self._close_native(handle)

    def flush_directory(self, directory: DirectoryHandle) -> None:
        if not isinstance(directory, _WindowsDirectoryHandle):
            raise TypeError("handle Windows attendu")
        if directory.closed:
            raise ValueError("handle de répertoire fermé")
        handle = self._open_native(
            self._path_string(directory.path),
            desired_access=_GENERIC_WRITE | _READ_CONTROL | _SYNCHRONIZE,
        )
        try:
            if not self._api.FlushFileBuffers(ctypes.c_void_p(handle)):
                _raise_code(ctypes.get_last_error(), "FlushFileBuffers", str(directory.path))
        finally:
            self._close_native(handle)

    def volume_space(self, directory: DirectoryHandle) -> VolumeSpace:
        total = ctypes.c_uint64()
        available = ctypes.c_uint64()
        free = ctypes.c_uint64()
        if not self._api.GetDiskFreeSpaceExW(
            self._extended_path(self._path_string(directory.path)),
            ctypes.byref(available),
            ctypes.byref(total),
            ctypes.byref(free),
        ):
            _raise_code(ctypes.get_last_error(), "GetDiskFreeSpaceExW", str(directory.path))
        if not total.value:
            raise UnsupportedFilesystemError("filesystem sans capacité mesurable")
        return VolumeSpace(int(total.value), int(available.value))

    def volume_identity(self, directory: DirectoryHandle) -> int:
        if not isinstance(directory, _WindowsDirectoryHandle):
            raise TypeError("handle Windows attendu")
        return directory.identity.volume

    def check_access(
        self,
        path: Path,
        *,
        read: bool = False,
        write: bool = False,
        execute: bool = False,
    ) -> bool:
        mode = 0
        if read:
            mode |= os.R_OK
        if write:
            mode |= os.W_OK
        if execute:
            mode |= os.X_OK
        return os.access(Path(path), mode)

    def path_version(self, path: Path) -> tuple[int, int, int] | None:
        path = Path(path)
        try:
            with self.open_directory(path.parent) as parent:
                info = self.entry_info(parent, path.name)
        except (FileNotFoundError, OSError, ValueError):
            return None
        if info is None or not info.is_regular or info.is_symlink or not self.is_owned(info):
            return None
        return (
            info.identity.file_id,
            info.modified_ns or 0,
            info.changed_ns or 0,
        )

    def owner_token(self) -> str:
        sid = self._current_user_sid_value()
        return hashlib.sha256(sid.encode("ascii")).hexdigest()[:24]

    def runtime_directory(self) -> Path:
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("USERPROFILE")
        if not root:
            raise UnsupportedFilesystemError("répertoire runtime Windows introuvable")
        return Path(root) / ".cache"

    def audit_permissions(self, path: Path, *, directory: bool) -> PermissionAudit:
        path = Path(path)
        handle = self._open_native(
            self._path_string(path),
            desired_access=_GENERIC_READ | _READ_CONTROL | _FILE_READ_ATTRIBUTES,
        )
        try:
            owner, private, detail = self._security_audit_handle(handle)
            return PermissionAudit(path, private, owner, None, detail)
        finally:
            self._close_native(handle)
