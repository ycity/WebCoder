"""Serve the agent (and the stdio bridge) as a pip-installable zip, so a device
can install it straight from the hub: pip install <hub>/downloads/webcoder-agent.zip"""
from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import Response

import webcoder
from webcoder import VERSION

AGENT_PYPROJECT = """[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "webcoder-agent"
version = "{version}"
description = "WebCoder agent and stdio bridge"
requires-python = ">=3.10"
dependencies = ["websockets>=15", "cryptography>=42", "regex>=2024.4"]

[project.scripts]
webcoder-agent = "webcoder.agent.__main__:main"
webcoder-bridge = "webcoder.bridge.__main__:main"
webcoder = "webcoder.cli.__main__:main"

[tool.setuptools]
packages = ["webcoder", "webcoder.common", "webcoder.agent", "webcoder.bridge", "webcoder.cli"]
"""


def agent_sources() -> dict[str, bytes]:
    package = Path(webcoder.__file__).resolve().parent
    files = {"webcoder/__init__.py": (package / "__init__.py").read_bytes()}
    for sub in ("common", "agent", "bridge", "cli"):
        for source in sorted((package / sub).glob("*.py")):
            files[f"webcoder/{sub}/{source.name}"] = source.read_bytes()
    return files


def agent_version(files: dict[str, bytes]) -> str:
    """VERSION plus a content hash, so re-running the installer upgrades exactly when the code changed
    (pip keeps an installed package whose version equals the one offered)."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\0" + files[name] + b"\0")
    return f"{VERSION}+{digest.hexdigest()[:12]}"


def build_agent_zip() -> bytes:
    files = agent_sources()
    version = agent_version(files)
    base = f"webcoder-agent-{version}/"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(base + "pyproject.toml", AGENT_PYPROJECT.format(version=version))
        for name, data in files.items():
            archive.writestr(base + name, data)
    return buffer.getvalue()


INSTALLERS = Path(__file__).resolve().parent / "installers"
EXTENSION = Path(__file__).resolve().parent / "extension"


def build_extension_zip() -> bytes:
    """The WebCoder browser extension, as a folder to unzip and load unpacked (Chrome / Edge)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(EXTENSION.rglob("*")):
            if source.is_file():
                archive.writestr("webcoder-extension/" + source.relative_to(EXTENSION).as_posix(), source.read_bytes())
    return buffer.getvalue()


def make_download_router() -> APIRouter:
    router = APIRouter()
    cache: dict[str, bytes] = {}

    @router.get("/downloads/webcoder-agent.zip")
    async def agent_zip():
        if "zip" not in cache:
            cache["zip"] = build_agent_zip()
        return Response(cache["zip"], media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="webcoder-agent-{VERSION}.zip"'})

    @router.get("/downloads/webcoder-extension.zip")
    async def extension_zip():
        if "extension" not in cache:
            cache["extension"] = build_extension_zip()
        return Response(cache["extension"], media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="webcoder-extension.zip"'})

    # Explicit UTF-8: Windows PowerShell 5.1's Invoke-RestMethod would otherwise misread the Chinese messages.
    @router.get("/downloads/install-agent.ps1")
    async def installer_windows():
        return Response((INSTALLERS / "install-agent.ps1").read_bytes(), media_type="text/plain; charset=utf-8")

    @router.get("/downloads/install-agent.sh")
    async def installer_unix():
        # bash chokes on CRLF (e.g. a hub deployed from a Windows checkout), so always serve LF.
        script = (INSTALLERS / "install-agent.sh").read_bytes().replace(b"\r\n", b"\n")
        return Response(script, media_type="text/x-shellscript; charset=utf-8")

    return router
