#!/usr/bin/env bash
# Run inside python:3.11-bullseye (Debian 11 / glibc 2.31) on GitHub Actions.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONUTF8=1

apt-get update
apt-get install -y --no-install-recommends \
  tk tcl libtk8.6 libtcl8.6 xvfb xauth fonts-noto-cjk \
  libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \
  libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 \
  libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
  libx11-6 libxcb1 libxext6 libxshmfence1 libgtk-3-0 ca-certificates file
python -m pip install --upgrade pip==24.3.1
python -m pip install -r requirements-linux-x64.txt
python -m pip check
python -c 'import tkinter; print("Tk:", tkinter.TkVersion)'

# Download only Chromium. It is placed alongside the executable for offline use.
python -m playwright install chromium
python -m unittest discover -s tests -p 'test_*.py' -v
TMIS_BROWSER_TESTS=1 xvfb-run -a python -m unittest discover -s tests -p 'test_report_browser.py' -v

python -m PyInstaller --noconfirm --clean --onedir --noupx \
  --name TMIS-Data-Free-Query --collect-all playwright \
  --hidden-import tkinter --hidden-import tkinter.simpledialog \
  'TMIS数据批量抓取工具V5.0_副本.py'

task_bundle='dist/TMIS-Data-Free-Query'
task_browser="$(python -c 'from playwright.sync_api import sync_playwright; p = sync_playwright().start(); print(p.chromium.executable_path); p.stop()')"
mkdir -p "$task_bundle/browser"
cp -a "$(dirname "$task_browser")" "$task_bundle/browser/chrome-linux"
cp 'scripts/launch-linux.sh' "$task_bundle/启动.sh"
chmod +x "$task_bundle/启动.sh"
cp 'outputs/kylin-stock-20260916/江苏库存自由查询_202501-202608_14库280任务.xlsx' "$task_bundle/"
cp 'LINUX麒麟V10使用说明.md' "$task_bundle/"
python -m pip freeze > "$task_bundle/build-dependencies.txt"
printf 'Git commit: %s\nBuild OS: Debian 11 (bullseye)\n' "${BUILD_COMMIT:-unknown}" > "$task_bundle/build-info.txt"
getconf GNU_LIBC_VERSION >> "$task_bundle/build-info.txt"
python --version >> "$task_bundle/build-info.txt"
file "$task_bundle/TMIS-Data-Free-Query" | tee "$task_bundle/architecture.txt"
file "$task_bundle/TMIS-Data-Free-Query" | grep -q 'ELF 64-bit.*x86-64'

# Test the distributed executable and the exact shipped browser, not just source imports.
xvfb-run -a "$task_bundle/启动.sh" --self-check

# tar preserves executable bits and PyInstaller's Linux symlinks.
tar -C dist -czf dist/TMIS-Kylin-V10-x64.tar.gz TMIS-Data-Free-Query
sha256sum dist/TMIS-Kylin-V10-x64.tar.gz > dist/TMIS-Kylin-V10-x64.tar.gz.sha256
