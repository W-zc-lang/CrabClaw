# -*- mode: python ; coding: utf-8 -*-
"""
CrabClaw.spec — PyInstaller onefile 打包配置

生成命令：
    pyinstaller CrabClaw.spec --clean --noconfirm

把递归深度上限提到 5000，避免分析 pywebview/pyautogui 依赖链时爆栈。
"""
import sys
sys.setrecursionlimit(5000)

from PyInstaller.building.build_main import Analysis, PYZ, EXE, BUNDLE


block_cipher = None

a = Analysis(
    ['gui.py'],
    pathex=['.'],
    binaries=[],
    datas=[('index.html', '.'), ('reward.jpg', '.'), ('icon.png', '.'), ('icon.ico', '.'), ('AUTHORS.txt', '.')],
    hiddenimports=[
        'mss',
        'pyautogui',
        'psutil',
        'json',
        'threading',
        'webview.platforms.edgechromium',
        'webview.platforms.edgehtml',
        'webview.http',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='CrabClaw',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    onefile=True,
    windowed=True,
    icon='icon.ico',
    version='version_info.txt',
)
