# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import runpy

repo_root = Path(SPECPATH)
build_info = runpy.run_path(str(repo_root / 'scripts' / 'generate_build_info.py'))['write_build_info'](repo_root)
pack_rtp = runpy.run_path(str(repo_root / 'scripts' / 'pack_rtp.py'))['pack_rtp_collection']
rtp_collection = pack_rtp(repo_root / 'modules' / 'RTPCollection')
module_data = [
    (str(path), f'modules/{path.name}' if path.is_dir() else 'modules')
    for path in sorted((repo_root / 'modules').iterdir())
    if path.name != 'RTPCollection'
]
module_data.append((str(rtp_collection), 'modules/RTPCollection'))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=module_data + [
        ('assets/icon.ico', 'assets'),
        (str(build_info), '.')
    ],
    hiddenimports=[
        'difflib',  # Imported by the external proofreading tool.
        'google.genai',
        'google.genai.types',
        'google.api_core.exceptions',
        'openai',
        'rubymarshal',
        'rubymarshal.reader',
        'rubymarshal.writer',
        'rubymarshal.classes',
        'rubymarshal.constants',
        'rubymarshal.utils',
        'winsdk.windows.data.xml.dom',
        'winsdk.windows.ui.notifications',
        'pythoncom',
        'win32com.shell.shell',
        'win32com.shell.shellcon',
        'win32com.propsys.propsys',
        'win32com.propsys.pscon',
        'openpyxl',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Not used by this app; excluding avoids PyInstaller failing on
        # incomplete Qt plugin installations in the global environment.
        'PyQt5',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='WindyTranslator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon='assets/icon.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='WindyTranslator',
)
