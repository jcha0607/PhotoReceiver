#!/usr/bin/env bash
# macOS용 .app 과 .dmg 를 만든다. 결과: dist/PhotoReceiver.dmg
# 필요: pip install -r requirements.txt (tkinter 가 포함된 Python)
set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf build dist
python3 scripts/write_build_info.py
python3 -m PyInstaller --windowed --noconfirm --name PhotoReceiver \
  --collect-all pillow_heif app.py

# DMG: .app 과 /Applications 바로가기를 함께 담는다
STAGE=$(mktemp -d)
cp -R dist/PhotoReceiver.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname PhotoReceiver -srcfolder "$STAGE" -ov -format UDZO dist/PhotoReceiver.dmg
rm -rf "$STAGE"
python3 scripts/make_manifest.py mac dist/PhotoReceiver.dmg dist/update-mac.json
echo "생성됨: dist/PhotoReceiver.dmg"
