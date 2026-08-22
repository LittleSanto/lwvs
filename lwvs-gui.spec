# -*- mode: python ; coding: utf-8 -*-
"""Recette PyInstaller de la GUI. `pyinstaller lwvs-gui.spec`

ONEDIR, PAS ONEFILE -- c'est un choix, pas un oubli :
  * onefile se re-extrait dans %TEMP% a CHAQUE lancement (demarrage lent) ;
  * il declenche beaucoup plus d'heuristiques antivirus, et ce programme
    ecoute deja le reseau : il part avec un handicap.

CONSOLE MASQUEE : l'utilisateur n'a rien a lire dans un terminal. En echange,
`packaging/lwvs_gui.py` attrape les exceptions de demarrage et les ecrit, sans
quoi un echec serait une fenetre qui n'apparait jamais.

`zstandard` charge son backend C par un import DYNAMIQUE (try/except dans son
__init__) : PyInstaller ne le voit pas tout seul. Sans cette ligne, le .exe se
rabat sur le backend CFFI -- ou ne demarre pas du tout.
"""

from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ['packaging/lwvs_gui.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=['zstandard.backend_c'] + collect_submodules('lwvs'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Rien de tout ca ne sert a la GUI, et chacun pese des dizaines de Mo.
    excludes=['pytest', 'numpy', 'pandas', 'matplotlib', 'PIL', 'setuptools',
              'pip', 'unittest', 'pydoc', 'doctest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='lwvs',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX = drapeau rouge supplementaire pour les antivirus
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='lwvs',
)
