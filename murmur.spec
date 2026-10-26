# Gnomist Praxis 1 · murmur.spec · MIT
"""PyInstaller build for Murmur.exe.

Build:
  python -m PyInstaller murmur.spec
Output:
  dist/Murmur.exe
"""

from PyInstaller.utils.hooks import collect_submodules

# ── Analysis ─────────────────────────
a = Analysis(
    ["murmur.py"],
    datas=[("assets/murmur.ico", "assets")],
    # winrt and pycaw load parts of themselves at runtime, out of the
    # analyzer's sight.
    hiddenimports=collect_submodules("winrt") + collect_submodules("pycaw"),
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)


# ── Executable ───────────────────────
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="Murmur",
    icon="assets/murmur.ico",
    console=False,
    upx=False,
)
