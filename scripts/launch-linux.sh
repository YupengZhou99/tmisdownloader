#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$task_root"
if [[ "$(uname -m)" != "x86_64" ]]; then
  printf '%s\n' '本软件包适用于 x86_64（X64），不能在 ARM/龙芯架构运行。' >&2
  exit 1
fi
task_glibc="$(getconf GNU_LIBC_VERSION | cut -d ' ' -f 2)"
if [[ "$(printf '%s\n' '2.31' "$task_glibc" | sort -V | head -n 1)" != '2.31' ]]; then
  printf '%s\n' "系统 glibc 为 $task_glibc，本包需要 2.31 或更新版本。" >&2
  exit 1
fi
if [[ -z "${DISPLAY:-}" ]]; then
  printf '%s\n' '请在麒麟图形桌面的终端中启动本程序（Tk 需要 DISPLAY/X11）。' >&2
  exit 1
fi
export PYTHONUTF8=1
exec "$task_root/TMIS-Data-Free-Query" "$@"
