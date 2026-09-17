#!/usr/bin/env bash
set -euo pipefail
if [ "$(uname -m)" != x86_64 ]; then
  echo '此包仅适用于 x64（x86_64）架构。'
  exit 1
fi
task_glibc="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')"
if [ -z "$task_glibc" ] || [ "$(printf '%s\n' 2.31 "$task_glibc" | sort -V | head -n 1)" != 2.31 ]; then
  echo '需要 glibc 2.31 或更新版本，请提供 ldd --version 第一行以确认系统兼容性。'
  exit 1
fi
if [ -z "${DISPLAY:-}" ]; then
  echo '需要图形桌面和 X11 / XWayland DISPLAY。请在麒麟桌面的终端中启动。'
  exit 1
fi
if [ "$(id -u)" = 0 ]; then
  echo '请使用普通桌面用户启动，不要使用 sudo 或 root。'
  exit 1
fi
task_directory="$(cd "$(dirname "$0")" && pwd)"
cd "$task_directory"
exec "$task_directory/tmis-workbench" "$@"
