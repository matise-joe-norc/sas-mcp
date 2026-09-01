"""Moving files between the SAS server and this machine.

SASPy transfers over the SAS connection itself, so this works when the two
machines share no filesystem at all -- SAS ODA runs in AWS and cannot see a
local disk, yet a workbook written there can still be fetched.

Transfers are confined to one directory. A download tool that accepted any
local path would let an agent overwrite anything the user can write, and an
upload tool that accepted any local path would let it send any readable file
to a remote server. Neither is a risk worth taking for the convenience of
skipping a copy, so both directions go through a single managed directory.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .session import SASSessionManager

# Conservative: what survives on both Windows and POSIX, with no separators.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


class TransferError(RuntimeError):
    """Raised when a transfer is refused or fails."""


def remote_basename(remote_path: str) -> str:
    """Take the filename from a remote path, whichever OS wrote it.

    The SAS server may be Windows or UNIX regardless of what this machine is,
    so both separators have to be understood.
    """
    cleaned = remote_path.strip().strip('"').strip("'")
    # A trailing separator means a directory was given. Path.name normalises
    # it away and would hand back the directory's own name as a filename.
    if cleaned.endswith(("/", "\\")):
        raise TransferError(
            f"{remote_path!r} looks like a directory, not a file. Give the "
            f"full path including the filename (list_sas_files can show it)."
        )
    name = PureWindowsPath(PurePosixPath(cleaned).name).name
    if not name or name in {".", ".."}:
        raise TransferError(f"Could not determine a filename from {remote_path!r}.")
    return name


def safe_local_name(name: str) -> str:
    """Validate a filename that will be created locally.

    Rejects separators, traversal, and anything exotic rather than trying to
    sanitise it -- a name that needs sanitising is a name worth refusing.
    """
    name = name.strip()
    if not _SAFE_NAME.match(name):
        raise TransferError(
            f"{name!r} is not an acceptable filename. Use letters, digits, "
            f"dot, dash, underscore or plus; no directory separators."
        )
    return name


def resolve_within(directory: Path, name: str) -> Path:
    """Resolve ``name`` inside ``directory``, refusing to escape it."""
    directory = directory.resolve()
    target = (directory / safe_local_name(name)).resolve()
    if target.parent != directory:
        raise TransferError("Refusing to write outside the transfer directory.")
    return target


def download(
    mgr: SASSessionManager, remote_path: str, local_name: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Fetch a file from the SAS server into the transfer directory."""
    sas = mgr.connect()
    name = safe_local_name(local_name) if local_name else remote_basename(remote_path)
    target = resolve_within(mgr.file_dir, name)

    if target.exists() and not overwrite:
        raise TransferError(
            f"{target} already exists. Pass overwrite=true to replace it, or "
            f"choose a different local_name."
        )

    try:
        result = sas.download(str(target), remote_path, overwrite=True)
    except Exception as exc:
        raise TransferError(f"Download failed: {exc}") from exc

    if not (result or {}).get("Success"):
        log = (result or {}).get("LOG", "")
        raise TransferError(
            f"SAS reported the download did not succeed. "
            f"Check that {remote_path!r} exists on the SAS server "
            f"(list_sas_files can confirm).\n{log[-400:]}"
        )
    if not target.exists():
        raise TransferError(
            f"SAS reported success but no local file appeared at {target}."
        )

    return {
        "status": "ok",
        "local_path": str(target),
        "remote_path": remote_path,
        "size_bytes": target.stat().st_size,
        "note": "The file is on this machine now and can be opened directly.",
    }


def upload(
    mgr: SASSessionManager, local_name: str, remote_path: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Send a file from the transfer directory to the SAS server.

    ``local_name`` is a filename inside the transfer directory, never a path:
    the agent should not be able to choose arbitrary files to send outward.
    """
    sas = mgr.connect()
    source = resolve_within(mgr.file_dir, local_name)
    if not source.is_file():
        raise TransferError(
            f"No file named {local_name!r} in the transfer directory "
            f"({mgr.file_dir}). Put it there first; uploads are restricted to "
            f"that directory."
        )

    try:
        result = sas.upload(str(source), remote_path, overwrite=overwrite)
    except Exception as exc:
        raise TransferError(f"Upload failed: {exc}") from exc

    if not (result or {}).get("Success"):
        log = (result or {}).get("LOG", "")
        raise TransferError(
            f"SAS reported the upload did not succeed.\n{log[-400:]}"
        )
    return {
        "status": "ok",
        "local_path": str(source),
        "remote_path": remote_path,
        "size_bytes": source.stat().st_size,
    }


def list_remote(mgr: SASSessionManager, path: str) -> dict[str, Any]:
    """List a directory on the SAS server."""
    sas = mgr.connect()
    try:
        entries = sas.dirlist(path)
    except Exception as exc:
        raise TransferError(f"Could not list {path!r}: {exc}") from exc
    return {
        "path": path,
        "entries": list(entries or []),
        "count": len(entries or []),
    }


def remote_info(mgr: SASSessionManager, path: str) -> dict[str, Any]:
    """Attributes of one file on the SAS server."""
    sas = mgr.connect()
    try:
        info = sas.file_info(path, quiet=True)
    except Exception as exc:
        raise TransferError(f"Could not stat {path!r}: {exc}") from exc
    if not info:
        raise TransferError(f"{path!r} was not found on the SAS server.")
    return {"path": path, "info": info}
