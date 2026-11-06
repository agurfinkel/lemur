"""Content-addressed solver inputs and provenance for saved sweeps."""

import hashlib
import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path


def preserve_input(source: Path, save_root: Path) -> dict[str, str]:
    """Snapshot input bytes atomically, sharing identical inputs across runs."""
    inputs = save_root / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    fd, temporary = tempfile.mkstemp(dir=inputs, prefix=".input-")
    try:
        with os.fdopen(fd, "wb") as dest, source.open("rb") as src:
            while chunk := src.read(1024 * 1024):
                digest.update(chunk)
                dest.write(chunk)
        sha256 = digest.hexdigest()
        relative = f"inputs/{sha256}.smt2"
        target = save_root / relative
        # Concurrent writers have identical bytes for this destination.
        if not target.exists():
            os.replace(temporary, target)
        return {"path": relative, "sha256": sha256}
    finally:
        Path(temporary).unlink(missing_ok=True)


@lru_cache(maxsize=16)
def solver_identity(executable: str) -> dict:
    """Record the version probe separately from the solver run's streams."""
    resolved = shutil.which(executable) or executable
    if os.path.dirname(resolved):
        resolved = str(Path(resolved).resolve())
    result = {"path": resolved, "version": None, "version_returncode": None}
    try:
        proc = subprocess.run(
            [resolved, "-version"], capture_output=True, text=True, timeout=5
        )
        result.update(
            version=proc.stdout.strip(),
            version_stderr=proc.stderr,
            version_returncode=proc.returncode,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["version_error"] = str(exc)
    return result
