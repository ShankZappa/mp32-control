#!/bin/bash
# Build MP32 Control.app (self-contained single app, fast startup). No terminal.
cd "$(dirname "$0")"
python3 -m pip install --user --upgrade pip
python3 -m pip install --user pywebview pyinstaller zeroconf
if [ -z "$MP32_SKIP_DEVICE_TEST" ]; then
  python3 device_preflight.py || exit 1
else
  echo "WARNING: physical MP32 preflight skipped by MP32_SKIP_DEVICE_TEST."
fi
python3 -m PyInstaller --windowed --noconfirm --clean \
  --name "MP32 Control" \
  --icon "assets/mp32-control.icns" \
  --collect-all zeroconf \
  --add-data "assets:assets" \
  --add-data "LICENSE:legal" \
  --add-data "NOTICE:legal" \
  --add-data "THIRD_PARTY_NOTICES.md:legal" \
  --osx-bundle-identifier "com.studio.mp32control" \
  app.py
rm -rf "dist/MP32 Control"   # remove redundant onedir folder; keep only the .app
PLIST="dist/MP32 Control.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName MP32 Control" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string 1.3.1" "$PLIST" 2>/dev/null || /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString 1.3.1" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string 1.3.1" "$PLIST" 2>/dev/null || /usr/libexec/PlistBuddy -c "Set :CFBundleVersion 1.3.1" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :NSHumanReadableCopyright string Independent MP32 Control project. Not affiliated with Antelope Audio." "$PLIST" 2>/dev/null || true

# A Developer ID certificate makes Gatekeeper trust distributable builds. Without one,
# ad-hoc signing still seals the bundle but macOS will show the first-launch warning.
if [ -n "$MP32_CODESIGN_IDENTITY" ]; then
  codesign --force --deep --options runtime --timestamp --sign "$MP32_CODESIGN_IDENTITY" "dist/MP32 Control.app"
  echo "Signed with Developer ID: $MP32_CODESIGN_IDENTITY"
else
  codesign --force --deep --sign - "dist/MP32 Control.app"
  echo "Ad-hoc signed (set MP32_CODESIGN_IDENTITY for a trusted Developer ID signature)."
fi
echo "Done -> dist/MP32 Control.app (move it anywhere; it's self-contained)"
