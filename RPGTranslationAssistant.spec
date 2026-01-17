# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('modules/EasyRPG', 'modules/EasyRPG'),
        ('modules/RPGRewriter', 'modules/RPGRewriter'),
        ('modules/RTPCollection', 'modules/RTPCollection'),
        ('modules/dict', 'modules/dict'),
        ('assets/icon.ico', 'assets')
    ],
    hiddenimports=[
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
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='RPGTranslationAssistant',
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
    name='RPGTranslationAssistant',
)
