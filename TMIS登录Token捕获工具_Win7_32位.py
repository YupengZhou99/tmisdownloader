#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TMIS 登录 Token URL 独立捕获工具。

设计目标：
- 运行于 Windows 7 SP1 32 位；
- 只依赖 Python 标准库和 psutil；
- 监控 TMIS 客户端新启动的浏览器进程；
- 捕获命令行中的登录 URL，可选择立即终止该浏览器以保留 Token；
- 在界面显示完整 URL，并支持自动/手动复制。

发布版使用 Python 3.8.10 x86 + PyInstaller 4.10 构建。
"""

import ntpath
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from urllib.parse import urlsplit

try:
    import psutil
except ImportError:
    psutil = None


APP_TITLE = "TMIS 登录 Token 捕获工具"
APP_VERSION = "1.0.0"
DEFAULT_KEYWORD = "tsas"
DEFAULT_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 0.01

BROWSER_PROCESS_NAMES = frozenset({
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "iexplore.exe",
    "360chrome.exe",
    "360se.exe",
    "qqbrowser.exe",
    "sogouexplorer.exe",
})

HTTP_URL_PATTERN = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)


def normalize_process_name(name):
    """把进程名或完整路径规范为小写文件名。"""
    if not name:
        return ""
    return ntpath.basename(str(name).strip()).lower()


def is_supported_browser(name):
    """判断进程是否属于需要监控的浏览器。"""
    return normalize_process_name(name) in BROWSER_PROCESS_NAMES


def extract_http_urls(cmdline):
    """从 psutil 命令行参数列表提取并去重 HTTP(S) URL。"""
    urls = []
    seen = set()
    for arg in cmdline or []:
        if not isinstance(arg, str):
            continue
        for match in HTTP_URL_PATTERN.finditer(arg):
            url = match.group(0).rstrip(",;)]}")
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def select_matching_url(cmdline, keyword=""):
    """返回第一个包含关键字的 URL；关键字为空时接受第一个 URL。"""
    normalized_keyword = str(keyword or "").strip().lower()
    for url in extract_http_urls(cmdline):
        if not normalized_keyword or normalized_keyword in url.lower():
            return url
    return None


def match_new_browser_process(pid, name, cmdline, known_pids, keyword=""):
    """纯函数：判断一个进程是否为本轮监控的捕获候选。"""
    if pid in known_pids or not is_supported_browser(name):
        return None
    return select_matching_url(cmdline, keyword)


def summarize_url(url):
    """生成不包含查询参数值的日志摘要，避免 Token 泄露到日志。"""
    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return "已捕获 URL（详情仅显示在结果框）"
        summary = "{}://{}{}".format(parts.scheme, parts.netloc, parts.path or "/")
        if parts.query:
            summary += "?..."
        return summary
    except Exception:
        return "已捕获 URL（详情仅显示在结果框）"


class TokenSniperApp(tk.Tk):
    """Windows 7 兼容的 Tkinter 图形界面。"""

    COLOR_BG = "#f3f5f7"
    COLOR_CARD = "#ffffff"
    COLOR_TEXT = "#20252b"
    COLOR_MUTED = "#5d6873"
    COLOR_BLUE = "#1f6fb2"
    COLOR_GREEN = "#18864b"
    COLOR_RED = "#b02a37"
    COLOR_WARN = "#9a6700"

    def __init__(self):
        tk.Tk.__init__(self)
        self.title("{} v{}".format(APP_TITLE, APP_VERSION))
        self.geometry("760x610")
        self.minsize(680, 540)
        self.configure(bg=self.COLOR_BG)

        self.keyword_var = tk.StringVar(value=DEFAULT_KEYWORD)
        self.timeout_var = tk.StringVar(value=str(DEFAULT_TIMEOUT_SECONDS))
        self.kill_browser_var = tk.BooleanVar(value=True)
        self.auto_copy_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="等待开始")

        self.stop_event = threading.Event()
        self.monitor_thread = None
        self.captured_url = ""
        self.is_monitoring = False

        self._init_styles()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if psutil is None:
            self.after(100, self._show_missing_dependency)

    def _init_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Card.TFrame", background=self.COLOR_CARD)
        style.configure(
            "Title.TLabel",
            background=self.COLOR_BG,
            foreground=self.COLOR_TEXT,
            font=("Microsoft YaHei UI", 16, "bold"),
        )
        style.configure(
            "Sub.TLabel",
            background=self.COLOR_BG,
            foreground=self.COLOR_MUTED,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Card.TLabel",
            background=self.COLOR_CARD,
            foreground=self.COLOR_TEXT,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Status.TLabel",
            background=self.COLOR_CARD,
            foreground=self.COLOR_BLUE,
            font=("Microsoft YaHei UI", 10, "bold"),
        )

    def _build_ui(self):
        outer = ttk.Frame(self, padding=(20, 16))
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(outer, text=APP_TITLE, style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(
            outer,
            text="独立版 | Windows 7 SP1 32 位 | 不启动 Playwright，不读取 Excel",
            style="Sub.TLabel",
        ).pack(anchor=tk.W, pady=(2, 12))

        settings = ttk.Frame(outer, style="Card.TFrame", padding=(16, 14))
        settings.pack(fill=tk.X)

        row1 = ttk.Frame(settings, style="Card.TFrame")
        row1.pack(fill=tk.X)
        ttk.Label(row1, text="URL 关键字", style="Card.TLabel", width=12).pack(side=tk.LEFT)
        self.keyword_entry = ttk.Entry(row1, textvariable=self.keyword_var)
        self.keyword_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 10))
        ttk.Label(
            row1,
            text="默认 tsas；抓不到时可清空",
            style="Card.TLabel",
        ).pack(side=tk.LEFT)

        row2 = ttk.Frame(settings, style="Card.TFrame")
        row2.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(row2, text="等待时间", style="Card.TLabel", width=12).pack(side=tk.LEFT)
        self.timeout_entry = ttk.Entry(row2, textvariable=self.timeout_var, width=10)
        self.timeout_entry.pack(side=tk.LEFT, padx=(8, 8))
        ttk.Label(row2, text="秒（10~3600）", style="Card.TLabel").pack(side=tk.LEFT)

        row3 = ttk.Frame(settings, style="Card.TFrame")
        row3.pack(fill=tk.X, pady=(10, 0))
        self.kill_browser_check = tk.Checkbutton(
            row3,
            text="捕获后终止新浏览器进程（用于避免 Token 被原浏览器消费）",
            variable=self.kill_browser_var,
            bg=self.COLOR_CARD,
            fg=self.COLOR_TEXT,
            activebackground=self.COLOR_CARD,
            highlightthickness=0,
            bd=0,
        )
        self.kill_browser_check.pack(side=tk.LEFT)
        self.auto_copy_check = tk.Checkbutton(
            row3,
            text="成功后自动复制",
            variable=self.auto_copy_var,
            bg=self.COLOR_CARD,
            fg=self.COLOR_TEXT,
            activebackground=self.COLOR_CARD,
            highlightthickness=0,
            bd=0,
        )
        self.auto_copy_check.pack(side=tk.LEFT, padx=(16, 0))

        warning = tk.Label(
            settings,
            text="注意：关键字为空时会捕获第一个新浏览器 URL；请在监控期间避免打开无关网页。",
            bg=self.COLOR_CARD,
            fg=self.COLOR_WARN,
            anchor=tk.W,
            font=("Microsoft YaHei UI", 9),
        )
        warning.pack(fill=tk.X, pady=(10, 0))

        controls = ttk.Frame(outer, padding=(0, 12))
        controls.pack(fill=tk.X)
        self.start_button = tk.Button(
            controls,
            text="开始监控",
            command=self.start_monitoring,
            bg=self.COLOR_GREEN,
            fg="white",
            activebackground="#126b3c",
            activeforeground="white",
            relief=tk.FLAT,
            padx=20,
            pady=7,
        )
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = tk.Button(
            controls,
            text="停止监控",
            command=self.stop_monitoring,
            bg=self.COLOR_RED,
            fg="white",
            activebackground="#8e202b",
            activeforeground="white",
            relief=tk.FLAT,
            padx=20,
            pady=7,
            state=tk.DISABLED,
        )
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))
        self.copy_button = tk.Button(
            controls,
            text="复制完整 URL",
            command=self.copy_captured_url,
            bg=self.COLOR_BLUE,
            fg="white",
            activebackground="#17598f",
            activeforeground="white",
            relief=tk.FLAT,
            padx=16,
            pady=7,
            state=tk.DISABLED,
        )
        self.copy_button.pack(side=tk.RIGHT)

        status_card = ttk.Frame(outer, style="Card.TFrame", padding=(16, 12))
        status_card.pack(fill=tk.X)
        ttk.Label(status_card, text="状态", style="Card.TLabel", width=10).pack(side=tk.LEFT)
        ttk.Label(status_card, textvariable=self.status_var, style="Status.TLabel").pack(side=tk.LEFT)

        ttk.Label(outer, text="捕获结果（包含敏感 Token，请勿外传）", style="Sub.TLabel").pack(
            anchor=tk.W, pady=(12, 4)
        )
        self.url_text = scrolledtext.ScrolledText(
            outer,
            height=7,
            wrap=tk.WORD,
            bg="white",
            fg=self.COLOR_TEXT,
            insertbackground=self.COLOR_TEXT,
            font=("Consolas", 9),
        )
        self.url_text.pack(fill=tk.BOTH, expand=False)

        ttk.Label(outer, text="运行日志（不会记录完整 Token）", style="Sub.TLabel").pack(
            anchor=tk.W, pady=(12, 4)
        )
        self.log_text = scrolledtext.ScrolledText(
            outer,
            height=8,
            wrap=tk.WORD,
            bg="#20252b",
            fg="#d8e0e7",
            font=("Consolas", 9),
            state=tk.DISABLED,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self._log("工具已就绪。点击“开始监控”后，再从 TMIS 客户端触发登录。")

    def _show_missing_dependency(self):
        messagebox.showerror(
            "缺少依赖",
            "未找到 psutil。源码运行请执行：pip install psutil==5.9.8\n"
            "正式 EXE 已内置该依赖。",
        )
        self.start_button.config(state=tk.DISABLED)

    def _log(self, message):
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, "[{}] {}\n".format(timestamp, message))
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _set_monitoring_state(self, monitoring):
        self.is_monitoring = monitoring
        self.start_button.config(state=tk.DISABLED if monitoring else tk.NORMAL)
        self.stop_button.config(state=tk.NORMAL if monitoring else tk.DISABLED)
        self.keyword_entry.config(state=tk.DISABLED if monitoring else tk.NORMAL)
        self.timeout_entry.config(state=tk.DISABLED if monitoring else tk.NORMAL)
        self.kill_browser_check.config(state=tk.DISABLED if monitoring else tk.NORMAL)
        self.auto_copy_check.config(state=tk.DISABLED if monitoring else tk.NORMAL)

    def start_monitoring(self):
        if psutil is None:
            self._show_missing_dependency()
            return
        if self.is_monitoring:
            return

        try:
            timeout_seconds = int(self.timeout_var.get().strip())
            if timeout_seconds < 10 or timeout_seconds > 3600:
                raise ValueError
        except ValueError:
            messagebox.showwarning("输入有误", "等待时间必须是 10 到 3600 之间的整数秒。")
            return

        keyword = self.keyword_var.get().strip()
        kill_browser = bool(self.kill_browser_var.get())
        auto_copy = bool(self.auto_copy_var.get())

        self.stop_event.clear()
        self.captured_url = ""
        self.url_text.delete("1.0", tk.END)
        self.copy_button.config(state=tk.DISABLED)
        self._set_monitoring_state(True)
        self.status_var.set("正在监控，请从 TMIS 客户端触发登录")
        self._log(
            "开始监控；关键字={}；超时={}秒；捕获后终止浏览器={}".format(
                keyword if keyword else "<空>",
                timeout_seconds,
                "是" if kill_browser else "否",
            )
        )

        self.monitor_thread = threading.Thread(
            target=self._monitor_worker,
            args=(keyword, timeout_seconds, kill_browser, auto_copy),
            daemon=True,
        )
        self.monitor_thread.start()

    def stop_monitoring(self):
        if not self.is_monitoring:
            return
        self.stop_event.set()
        self.status_var.set("正在停止...")
        self._log("已请求停止监控。")

    def _browser_pid_snapshot(self):
        known_pids = set()
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if is_supported_browser(proc.info.get("name")):
                    known_pids.add(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
                continue
        return known_pids

    def _monitor_worker(self, keyword, timeout_seconds, kill_browser, auto_copy):
        try:
            known_pids = self._browser_pid_snapshot()
        except Exception as exc:
            error_message = "读取进程列表失败：{}".format(exc)
            self.after(0, lambda message=error_message: self._monitor_failed(message))
            return

        self.after(0, lambda: self._log("已忽略 {} 个监控开始前存在的浏览器进程。".format(len(known_pids))))
        deadline = time.monotonic() + timeout_seconds

        while not self.stop_event.is_set() and time.monotonic() < deadline:
            try:
                for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                    if self.stop_event.is_set():
                        break
                    try:
                        info = proc.info
                        url = match_new_browser_process(
                            info.get("pid"),
                            info.get("name"),
                            info.get("cmdline"),
                            known_pids,
                            keyword,
                        )
                        if not url:
                            continue

                        killed = False
                        kill_error = ""
                        if kill_browser:
                            try:
                                proc.kill()
                                killed = True
                            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as exc:
                                kill_error = str(exc)

                        self.stop_event.set()
                        self.after(
                            0,
                            lambda captured=url, was_killed=killed, error=kill_error: self._capture_succeeded(
                                captured, kill_browser, was_killed, error, auto_copy
                            ),
                        )
                        return
                    except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, TypeError):
                        continue
            except Exception:
                # 某一轮进程枚举失败不应终止整个监控。
                pass

            self.stop_event.wait(POLL_INTERVAL_SECONDS)

        if self.stop_event.is_set():
            self.after(0, self._monitor_stopped)
        else:
            self.after(0, self._monitor_timed_out)

    def _capture_succeeded(self, url, kill_attempted, killed, kill_error, auto_copy):
        self.captured_url = url
        self.url_text.delete("1.0", tk.END)
        self.url_text.insert("1.0", url)
        self.copy_button.config(state=tk.NORMAL)
        self._set_monitoring_state(False)
        self.status_var.set("捕获成功")
        self._log("捕获成功：{}".format(summarize_url(url)))

        if kill_attempted:
            if killed:
                self._log("已终止携带 URL 的新浏览器进程。")
            else:
                self._log("未能终止新浏览器进程：{}".format(kill_error or "未知原因"))

        if auto_copy:
            if self._copy_to_clipboard(url):
                self._log("完整 URL 已复制到剪贴板。")
            else:
                self._log("自动复制失败，请点击“复制完整 URL”。")

        self.bell()

    def _monitor_failed(self, message):
        self._set_monitoring_state(False)
        self.status_var.set("监控失败")
        self._log(message)
        messagebox.showerror("监控失败", message)

    def _monitor_stopped(self):
        if not self.is_monitoring:
            return
        self._set_monitoring_state(False)
        self.status_var.set("已停止")
        self._log("监控已停止，未捕获 URL。")

    def _monitor_timed_out(self):
        self._set_monitoring_state(False)
        self.status_var.set("等待超时")
        self._log("等待超时，未捕获符合条件的 URL。")

    def _copy_to_clipboard(self, value):
        try:
            self.clipboard_clear()
            self.clipboard_append(value)
            self.update()
            return True
        except tk.TclError:
            return False

    def copy_captured_url(self):
        if not self.captured_url:
            messagebox.showinfo("没有结果", "当前没有可复制的 URL。")
            return
        if self._copy_to_clipboard(self.captured_url):
            self._log("完整 URL 已手动复制到剪贴板。")
            self.status_var.set("已复制到剪贴板")
        else:
            messagebox.showerror("复制失败", "无法访问系统剪贴板，请手动选择结果框内容复制。")

    def _on_close(self):
        self.stop_event.set()
        self.destroy()


def main():
    if sys.version_info < (3, 8):
        raise RuntimeError("本工具要求 Python 3.8 或更高版本；Win7 发布版固定使用 Python 3.8.10。")
    app = TokenSniperApp()
    app.mainloop()


if __name__ == "__main__":
    main()
