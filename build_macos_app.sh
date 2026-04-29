#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="X Media Downloader"
APP_BUNDLE_DIR="$SCRIPT_DIR/dist/$APP_NAME.app"
APP_CONTENTS_DIR="$APP_BUNDLE_DIR/Contents"
APP_MACOS_DIR="$APP_CONTENTS_DIR/MacOS"
APP_RESOURCES_DIR="$APP_CONTENTS_DIR/Resources"
APP_SUPPORT_SOURCE_DIR="$APP_RESOURCES_DIR/app"
ICON_PNG_PATH="${ICON_PNG_PATH:-$SCRIPT_DIR/logo.png}"
ICON_ICNS_PATH="${ICON_ICNS_PATH:-$SCRIPT_DIR/logo.icns}"

can_run_tk() {
  local python_bin="$1"
  "$python_bin" -c 'import tkinter as tk; root = tk.Tk(); root.withdraw(); print(root.tk.eval("info patchlevel")); root.destroy()' >/dev/null 2>&1
}

resolve_build_python() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    if [[ ! -x "${PYTHON_BIN}" ]]; then
      echo "error: PYTHON_BIN is set but not executable: ${PYTHON_BIN}" >&2
      exit 1
    fi
    printf '%s\n' "${PYTHON_BIN}"
    return 0
  fi

  local candidates=(
    /opt/homebrew/bin/python3.12
    /opt/homebrew/bin/python3.13
    /opt/homebrew/bin/python3.14
    /opt/homebrew/bin/python3
    /usr/local/bin/python3.12
    /usr/local/bin/python3.13
    /usr/local/bin/python3.14
    /usr/local/bin/python3
    "$(command -v python3 || true)"
    "$(command -v python || true)"
  )
  local candidate

  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" ]] || continue
    [[ -x "$candidate" ]] || continue
    if can_run_tk "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  echo "error: no usable Python with tkinter support was found." >&2
  echo "hint: install Homebrew python plus python-tk, or set PYTHON_BIN explicitly." >&2
  exit 1
}

build_icns_from_png() {
  local source_png="$1"
  local output_icns="$2"
  local iconset_dir
  local width
  local height

  width="$(sips -g pixelWidth "$source_png" 2>/dev/null | awk '/pixelWidth/ {print $2}')"
  height="$(sips -g pixelHeight "$source_png" 2>/dev/null | awk '/pixelHeight/ {print $2}')"

  if [[ -z "$width" || -z "$height" ]]; then
    echo "error: could not read PNG dimensions from $source_png" >&2
    exit 1
  fi

  if (( width < 1024 || height < 1024 )); then
    echo "note: icon source is ${width}x${height}; macOS app icons look best at 1024x1024 or larger." >&2
  fi

  iconset_dir="$(mktemp -d "${TMPDIR:-/tmp}/x-media-icon.XXXXXX.iconset")"
  trap 'rm -rf "$iconset_dir"' RETURN

  sips -z 16 16 "$source_png" --out "$iconset_dir/icon_16x16.png" >/dev/null
  sips -z 32 32 "$source_png" --out "$iconset_dir/icon_16x16@2x.png" >/dev/null
  sips -z 32 32 "$source_png" --out "$iconset_dir/icon_32x32.png" >/dev/null
  sips -z 64 64 "$source_png" --out "$iconset_dir/icon_32x32@2x.png" >/dev/null
  sips -z 128 128 "$source_png" --out "$iconset_dir/icon_128x128.png" >/dev/null
  sips -z 256 256 "$source_png" --out "$iconset_dir/icon_128x128@2x.png" >/dev/null
  sips -z 256 256 "$source_png" --out "$iconset_dir/icon_256x256.png" >/dev/null
  sips -z 512 512 "$source_png" --out "$iconset_dir/icon_256x256@2x.png" >/dev/null
  sips -z 512 512 "$source_png" --out "$iconset_dir/icon_512x512.png" >/dev/null
  sips -z 1024 1024 "$source_png" --out "$iconset_dir/icon_512x512@2x.png" >/dev/null

  iconutil -c icns "$iconset_dir" -o "$output_icns"
  rm -rf "$iconset_dir"
  trap - RETURN
}

create_launcher() {
  local launcher_path="$1"
  local python_bin="$2"

  cat >"$launcher_path" <<EOF
#!/bin/zsh
set -euo pipefail

APP_DIR="\$(cd "\$(dirname "\$0")/.." && pwd)"
RESOURCE_DIR="\$APP_DIR/Resources/app"
LOG_DIR="\$HOME/Library/Logs/X Media Downloader"
LOG_FILE="\$LOG_DIR/launcher.log"
PYTHON_BIN="$python_bin"
BOOTSTRAP_PYTHON="/usr/bin/python3"

if [[ ! -x "\$BOOTSTRAP_PYTHON" ]]; then
  BOOTSTRAP_PYTHON="\$PYTHON_BIN"
fi

mkdir -p "\$LOG_DIR"
export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"
export PYTHONUNBUFFERED=1
export PYTHON_FOR_VENV="\$BOOTSTRAP_PYTHON"

exec "\$PYTHON_BIN" "\$RESOURCE_DIR/download_x_media_gui.py" >>"\$LOG_FILE" 2>&1
EOF

  chmod +x "$launcher_path"
}

create_info_plist() {
  local plist_path="$1"

  cat >"$plist_path" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleDisplayName</key>
  <string>$APP_NAME</string>
  <key>CFBundleExecutable</key>
  <string>$APP_NAME</string>
  <key>CFBundleIconFile</key>
  <string>logo.icns</string>
  <key>CFBundleIdentifier</key>
  <string>local.xmediadownloader</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>$APP_NAME</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSApplicationCategoryType</key>
  <string>public.app-category.utilities</string>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
EOF
}

cd "$SCRIPT_DIR"
PYTHON_BIN="$(resolve_build_python)"
echo "Using launcher Python: $PYTHON_BIN"

if [[ -f "$ICON_ICNS_PATH" ]]; then
  :
elif [[ -f "$ICON_PNG_PATH" ]]; then
  build_icns_from_png "$ICON_PNG_PATH" "$ICON_ICNS_PATH"
else
  echo "error: no logo.icns or logo.png found." >&2
  exit 1
fi

rm -rf "$APP_BUNDLE_DIR"
mkdir -p "$APP_MACOS_DIR" "$APP_SUPPORT_SOURCE_DIR"

cp "$SCRIPT_DIR/download_x_media.py" "$APP_SUPPORT_SOURCE_DIR/"
cp "$SCRIPT_DIR/download_x_media_gui.py" "$APP_SUPPORT_SOURCE_DIR/"
cp "$ICON_ICNS_PATH" "$APP_RESOURCES_DIR/logo.icns"

create_launcher "$APP_MACOS_DIR/$APP_NAME" "$PYTHON_BIN"
create_info_plist "$APP_CONTENTS_DIR/Info.plist"
printf 'APPL????' >"$APP_CONTENTS_DIR/PkgInfo"

echo
echo "Built app bundle:"
echo "  $APP_BUNDLE_DIR"
echo
echo "Launcher runtime:"
echo "  $PYTHON_BIN"
echo
echo "Logs:"
echo "  \$HOME/Library/Logs/X Media Downloader/launcher.log"
