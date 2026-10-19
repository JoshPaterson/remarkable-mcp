"""Saving exports to folders on this machine that the configuration allows.

``REMARKABLE_EXPORT_DIRS`` lists the folders (separated by ``os.pathsep``,
``:`` on macOS and Linux). A save path must resolve, symlinks included, to a
file inside one of them. Files are written to a temporary name in the target
folder and renamed into place, so a reader never sees a partial file, and an
existing file is only replaced when asked. Nothing else is ever removed.
"""

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from pathlib import Path
from typing import List

EXPORT_DIRS_ENV = "REMARKABLE_EXPORT_DIRS"
SUFFIXES = {"pdf": ".pdf", "markdown": ".md"}


class SaveError(Exception):
    def __init__(self, error_type: str, message: str, suggestion: str):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.suggestion = suggestion


def allowed_dirs() -> List[Path]:
    """The folders exports may be saved in, resolved."""
    raw = os.environ.get(EXPORT_DIRS_ENV, "")
    dirs = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        path = Path(part).expanduser()
        if path.is_absolute():
            dirs.append(Path(os.path.realpath(path)))
    return dirs


def check_path(save_to: str, output_format: str, overwrite: bool) -> Path:
    """The resolved path to save an export at, or a SaveError."""
    dirs = allowed_dirs()
    if not dirs:
        raise SaveError(
            "save_not_configured",
            f"Saving exports is not enabled: {EXPORT_DIRS_ENV} lists no folders.",
            f"Set {EXPORT_DIRS_ENV} in the connector's configuration to the folders "
            "exports may be saved in, or omit save_to for a temporary resource.",
        )
    if not isinstance(save_to, str) or not save_to.strip() or "\0" in save_to:
        raise SaveError("invalid_save_path", "save_to must be a file path.", _allowed_hint(dirs))
    path = Path(save_to).expanduser()
    if not path.is_absolute():
        raise SaveError(
            "invalid_save_path",
            f"save_to must be an absolute path: '{save_to}'.",
            _allowed_hint(dirs),
        )
    suffix = SUFFIXES[output_format]
    if path.suffix.lower() != suffix:
        raise SaveError(
            "invalid_save_path",
            f"A {output_format} export must be saved with the {suffix} extension.",
            f"End save_to with '{suffix}'.",
        )
    resolved = Path(os.path.realpath(path))
    if not any(resolved.parent == d or d in resolved.parents for d in dirs):
        raise SaveError(
            "save_path_not_allowed",
            f"'{save_to}' is outside the folders exports may be saved in.",
            _allowed_hint(dirs),
        )
    if os.path.lexists(path):
        if path.is_symlink() or not resolved.is_file():
            raise SaveError(
                "invalid_save_path",
                f"'{save_to}' exists and is not a regular file.",
                "Choose another file name.",
            )
        if not overwrite:
            raise SaveError(
                "file_exists",
                f"'{save_to}' already exists.",
                "Pass overwrite=true to replace it, or choose another file name.",
            )
    return resolved


def _allowed_hint(dirs: List[Path]) -> str:
    return "Save inside one of: " + ", ".join(str(d) for d in dirs)


def save(data: bytes, path: Path, overwrite: bool) -> dict:
    """Write ``data`` to ``path`` atomically; returns its path, size and SHA-256.

    Missing parent folders are created. Without ``overwrite`` an existing file
    is never replaced, even one created while saving.
    """
    dirs = allowed_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = Path(os.path.realpath(path.parent))
    if not any(parent == d or d in parent.parents for d in dirs):
        raise SaveError(
            "save_path_not_allowed",
            f"'{path}' is outside the folders exports may be saved in.",
            _allowed_hint(dirs),
        )
    fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=f".{path.name}.", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        target = parent / path.name
        if overwrite:
            os.replace(tmp, target)
        else:
            try:
                os.link(tmp, target)
            except FileExistsError:
                raise SaveError(
                    "file_exists",
                    f"'{target}' already exists.",
                    "Pass overwrite=true to replace it, or choose another file name.",
                ) from None
            except OSError as e:
                if e.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV):
                    raise
                # No hard links here: rename, after checking the name is free.
                if os.path.lexists(target):
                    raise SaveError(
                        "file_exists",
                        f"'{target}' already exists.",
                        "Pass overwrite=true to replace it, or choose another file name.",
                    ) from None
                os.rename(tmp, target)
    finally:
        # The temporary file is this call's own; it is gone once renamed.
        if tmp.exists():
            tmp.unlink()
    written = target.read_bytes()
    return {
        "saved_path": str(target),
        "size": len(written),
        "sha256": hashlib.sha256(written).hexdigest(),
    }
