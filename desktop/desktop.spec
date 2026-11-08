# Run from repository root: pyinstaller --noconfirm desktop/desktop.spec
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
packages = ('tradingagents', 'langchain_openai', 'langchain_anthropic',
            'langchain_google_genai', 'langgraph', 'keyring.backends')
hidden = [name for package in packages for name in collect_submodules(package)]
# AKShare resolves its calendar package dynamically through importlib.resources.
hidden += collect_submodules('akshare.data')
data = collect_data_files('tradingagents') + collect_data_files('akshare')
for distribution_name in ('tradingagents', 'pywebview', 'keyring', 'akshare', 'pypdf'):
    data += copy_metadata(distribution_name, recursive=True)
data += [(str(root / 'LICENSE'), 'licenses/TradingAgents')]
# Third-party distribution metadata includes license files when available.
a = Analysis([str(root / 'desktop/launcher.py')], pathex=[str(root)],
             binaries=[], datas=data, hiddenimports=hidden,
             excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'pytest', 'IPython', 'notebook'],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='StockSeek',
          icon=str(root / 'desktop/assets' / ('app.icns' if sys.platform == 'darwin' else 'app.ico')),
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=sys.platform != 'win32', argv_emulation=False)
executables = [exe]
if sys.platform == 'win32':
    worker = EXE(pyz, a.scripts, [], exclude_binaries=True, name='StockSeekWorker',
                 console=True, strip=False, upx=False)
    executables.append(worker)
collection = COLLECT(*executables, a.binaries, a.datas, strip=False, upx=False, name='StockSeek')
if sys.platform == 'darwin':
    app = BUNDLE(collection, name='StockSeek.app', bundle_identifier='ai.tradingagents.yanzong',
                 icon=str(root / 'desktop/assets/app.icns'),
                 info_plist={'CFBundleDisplayName': 'StockSeek', 'NSHighResolutionCapable': True})
