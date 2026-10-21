# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for packaging Verifyarr into a standalone onedir Windows build."""

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

project_root = Path(SPECPATH).resolve()

# Bundle templates, optional static assets, and config.example.yaml
datas = [
    (str(project_root / "src" / "templates"), "src/templates"),
    (str(project_root / "config.example.yaml"), "."),
]

static_dir = project_root / "src" / "static"
if static_dir.exists():
    datas.append((str(static_dir), "src/static"))

datas += collect_data_files("certifi")

hiddenimports = [
    # Application modules
    "src",
    "src.main",
    "src.config",
    "src.models",
    "src.models.servarr",
    "src.models.history",
    "src.routers",
    "src.routers.settings",
    "src.services",
    "src.services.prober",
    "src.services.visual_check",
    "src.services.verifier",
    "src.services.remediator",
    "src.services.notifier",
    "src.utils",
    "src.utils.path_mapper",
    # Uvicorn dynamic imports (event loops, protocols, lifespans, logging)
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # FastAPI / Starlette / Jinja2 / Pydantic / HTTPX / OCR
    "fastapi",
    "starlette",
    "jinja2",
    "pydantic",
    "pydantic_settings",
    "yaml",
    "httpx",
    "httpcore",
    "anyio",
    "anyio._backends._asyncio",
    "PIL",
    "pytesseract",
    "sqlite3",
]

hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("pydantic")
hiddenimports += collect_submodules("pydantic_settings")

a = Analysis(
    [str(project_root / "src" / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "mypy", "ruff", "black"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="verifyarr",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="verifyarr",
)

