"""Standard-library-only notebook client. Application packages stay in a venv.

This file is imported by the Colab kernel. It must never import publicmail,
Playwright, FastAPI, pandas, or any other third-party application dependency.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path


def install_runtime(repository: Path, root: Path, *, browser: bool) -> tuple[Path, dict]:
    if sys.version_info < (3, 11):  # noqa: UP036 — runs before application dependencies are installed
        raise RuntimeError("Select a Colab Python runtime with Python 3.11 or newer.")
    if shutil.disk_usage(root).free < (4 if browser else 2) * 1024**3:
        raise RuntimeError("Not enough free local disk for installation. Start a fresh runtime.")
    environment = dict(os.environ)
    # Colab can inject kernel-specific packages via PYTHONPATH. Do not let those
    # shadow the private application's pinned dependencies or Python prefix.
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment.update(
        PYTHONUNBUFFERED="1",
        PLAYWRIGHT_BROWSERS_PATH=str(root / "publicmail-browsers"),
        XDG_CACHE_HOME=str(root / "publicmail-cache"),
        PIP_DISABLE_PIP_VERSION_CHECK="1",
    )
    virtualenv = root / "publicmail-venv"
    python = virtualenv / "bin" / "python"

    def call(*args):
        subprocess.run([str(a) for a in args], check=True, env=environment)

    if not python.is_file():
        # Colab may not include ensurepip. Host pip's --python option bootstraps
        # pip into this venv without installing/upgrading ANY kernel packages.
        call(sys.executable, "-m", "venv", "--without-pip", virtualenv)
        call(sys.executable, "-m", "pip", "--python", python, "install", "pip>=24,<27")
    call(python, "-m", "pip", "install", "-r", repository / "requirements.lock")
    if browser:
        call(python, "-m", "pip", "install", "-r", repository / "requirements-browser.lock")
    call(python, "-m", "pip", "install", "--no-deps", repository)
    call(python, "-m", "pip", "check")
    if browser:
        call(python, "-m", "playwright", "install", "--with-deps", "chromium")
    return python, environment


class ColabClient:
    def __init__(
        self,
        python: Path,
        work_dir: Path,
        *,
        drive_dir: Path | None = None,
        drive_mount: Path | None = None,
        environment: dict | None = None,
    ):
        self.python = Path(python)
        self.work_dir = Path(work_dir)
        self.drive_dir = Path(drive_dir) if drive_dir else None
        self.drive_mount = Path(drive_mount) if drive_mount else None
        self.environment = environment or dict(os.environ)
        self._active = None

    def command(self, *args) -> list[str]:
        result = [str(self.python), "-m", "publicmail.colab", "--work-dir", str(self.work_dir)]
        if self.drive_dir:
            result += ["--drive-dir", str(self.drive_dir)]
        if self.drive_mount:
            result += ["--drive-mount", str(self.drive_mount)]
        return result + [str(a) for a in args]

    def _idle(self):
        if self._active is not None and self._active.poll() is None:
            raise RuntimeError("The previous worker is still stopping. Wait before running another command.")

    def json(self, *args, deadline_seconds: float = 600) -> dict:
        self._idle()
        try:
            result = subprocess.run(
                self.command(*args),
                check=True,
                capture_output=True,
                text=True,
                env=self.environment,
                timeout=deadline_seconds,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError((exc.stderr or exc.stdout or "Notebook command failed")[-4000:]) from exc
        if result.stderr:
            print(result.stderr, end="")
        return json.loads(result.stdout)

    def run(self, *args) -> None:
        """Foreground cell; Interrupt requests a graceful checkpoint/export."""
        self._idle()
        process = subprocess.Popen(
            self.command(*args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=self.environment,
            start_new_session=True,
        )
        self._active = process
        try:
            for line in process.stdout:
                print(line, end="", flush=True)
            result = process.wait()
        except KeyboardInterrupt:
            print("\nStopping new claims; waiting for active work, final backup and exports…", flush=True)
            process.send_signal(signal.SIGINT)
            try:
                remaining, _ = process.communicate(timeout=420)
                if remaining:
                    print(remaining, end="")
                result = process.returncode
            except subprocess.TimeoutExpired as exc:
                # Never silently SIGKILL an exporter/backup or start a duplicate.
                raise RuntimeError(
                    "The worker is still stopping. Do not start a second run. "
                    "Wait, then retry the status/export cell. Last complete Drive snapshots remain usable."
                ) from exc
        finally:
            if process.poll() is not None and process.stdout:
                process.stdout.close()
        if result:
            raise RuntimeError(
                f"Worker exited with code {result}. Read the error above. Local results and previous "
                "verified backups were kept; the export/download cell can still recover a local ZIP."
            )
