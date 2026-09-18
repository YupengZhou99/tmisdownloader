#!/usr/bin/env bash
# Build and execute the shipped app in Debian 11 / glibc 2.31.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive PYTHONUTF8=1 PIP_DISABLE_PIP_VERSION_CHECK=1
cp scripts/debian-bullseye-snapshot.list /etc/apt/sources.list
apt-get -o Acquire::Retries=3 update
apt-get install -y --no-install-recommends \
  tk tcl libtk8.6 libtcl8.6 xvfb xauth fonts-noto-cjk curl xz-utils \
  libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \
  libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 \
  libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
  libx11-6 libxcb1 libxext6 libxshmfence1 libgtk-3-0 ca-certificates file binutils
task_node_dir="$(mktemp -d)"
curl -fL --retry 3 https://nodejs.org/dist/v22.22.2/node-v22.22.2-linux-x64.tar.xz -o "$task_node_dir/node.tar.xz"
curl -fL --retry 3 https://nodejs.org/dist/v22.22.2/SHASUMS256.txt -o "$task_node_dir/SHASUMS256.txt"
task_node_sha="$(awk '$2=="node-v22.22.2-linux-x64.tar.xz" {print $1}' "$task_node_dir/SHASUMS256.txt")"
test "${#task_node_sha}" = 64
printf '%s  %s\n' "$task_node_sha" "$task_node_dir/node.tar.xz" | sha256sum -c -
tar -xJf "$task_node_dir/node.tar.xz" -C "$task_node_dir"
export PATH="$task_node_dir/node-v22.22.2-linux-x64/bin:$PATH"
python -m pip install --upgrade pip==24.3.1
python -m pip install -r requirements-linux-x64.txt
python -m pip check
python -m playwright install chromium
python -m unittest discover -s tests -p 'test_*.py' -v
TMIS_BROWSER_TESTS=1 xvfb-run -a python -m unittest discover -s tests -p 'test_report_browser.py' -v
TMIS_GUI_TESTS=1 xvfb-run -a python -m unittest discover -s tests -p 'test_report_ui.py' -v
npm --prefix desktop ci --registry=https://registry.npmjs.org --no-audit --no-fund
node desktop/node_modules/electron/install.js
test -x desktop/node_modules/electron/dist/electron
npm --prefix desktop run build
npm --prefix desktop test

python -m PyInstaller --noconfirm --clean --onedir --noupx \
  --name tmis-worker --collect-all playwright --hidden-import tmis_ui \
  --hidden-import tkinter.simpledialog --hidden-import pyperclip --hidden-import psutil --hidden-import openpyxl \
  --add-data 'TMIS数据批量抓取工具V5.0_副本.py:.' tmis_worker.py
task_bundle='dist/TMIS-Workbench-Kylin-x64'
mkdir -p "$task_bundle"
cp -a desktop/node_modules/electron/dist/. "$task_bundle/"
mv "$task_bundle/electron" "$task_bundle/tmis-workbench"
mkdir -p "$task_bundle/resources/app"
cp -a desktop/dist desktop/electron desktop/package.json "$task_bundle/resources/app/"
cp -a dist/tmis-worker "$task_bundle/resources/backend"
task_browser="$(python -c 'from playwright.sync_api import sync_playwright; p = sync_playwright().start(); print(p.chromium.executable_path); p.stop()')"
mkdir -p "$task_bundle/resources/backend/browser"
cp -a "$(dirname "$task_browser")" "$task_bundle/resources/backend/browser/chrome-linux"
cp scripts/launch-desktop-linux.sh "$task_bundle/启动.sh"
chmod +x "$task_bundle/启动.sh" "$task_bundle/tmis-workbench"
cp DESKTOP_V6.md "$task_bundle/使用说明.md"
python -m pip freeze > "$task_bundle/build-dependencies.txt"
printf 'Git commit: %s\nTarget: Kylin V10 SP1 x64\nBuild: Debian 11 snapshot 20260801T000000Z\n' "${BUILD_COMMIT:-unknown}" > "$task_bundle/build-info.txt"
getconf GNU_LIBC_VERSION >> "$task_bundle/build-info.txt"
node --version >> "$task_bundle/build-info.txt"
node -p 'require("./desktop/node_modules/electron/package.json").version' >> "$task_bundle/build-info.txt"
file "$task_bundle/tmis-workbench" "$task_bundle/resources/backend/tmis-worker" "$task_bundle/resources/backend/browser/chrome-linux/chrome" | tee "$task_bundle/architecture.txt"
file "$task_bundle/tmis-workbench" | grep -q 'ELF 64-bit.*x86-64'
file "$task_bundle/resources/backend/tmis-worker" | grep -q 'ELF 64-bit.*x86-64'
ldd "$task_bundle/tmis-workbench" > "$task_bundle/system-libraries.txt"
if grep -q 'not found' "$task_bundle/system-libraries.txt"; then
  cat "$task_bundle/system-libraries.txt"
  exit 1
fi
export TMIS_PYTHON="$(command -v python)"
export TMIS_PLAYWRIGHT_NODE="$(python -c 'import pathlib,playwright;print(pathlib.Path(playwright.__file__).parent / "driver/package")')"
export TMIS_DESKTOP_EXECUTABLE="$PWD/$task_bundle/tmis-workbench"
# Only the synthetic root-container test uses this; the launcher keeps sandboxing.
TMIS_CI_ROOT=1 xvfb-run -a node desktop/scripts/smoke.cjs
TMIS_CI_ROOT=1 xvfb-run -a node desktop/scripts/workspaces-smoke.cjs
TMIS_CI_ROOT=1 TMIS_SOAK_SECONDS=14400 TMIS_SOAK_DOWNLOADS=1000 xvfb-run -a node desktop/scripts/stability-soak.cjs
cp output/playwright/desktop-smoke.json "$task_bundle/acceptance.json"
cp output/playwright/workspaces-smoke.json "$task_bundle/workspaces-acceptance.json"
cp output/playwright/stability-soak.json "$task_bundle/stability-acceptance.json"
cp DESKTOP_V6.md dist/使用说明.md
cp output/playwright/workspaces-smoke.json dist/验收结果.json
cp output/playwright/stability-soak.json dist/长稳验收结果.json
tar -C dist -czf dist/TMIS-Workbench-V6.2-Kylin-x64.tar.gz TMIS-Workbench-Kylin-x64
sha256sum dist/TMIS-Workbench-V6.2-Kylin-x64.tar.gz | sed 's@  dist/@  @' | tee dist/TMIS-Workbench-V6.2-Kylin-x64.tar.gz.sha256
