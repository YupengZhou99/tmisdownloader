#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
国库收支存明细表批量下载工具
=============================
菜单路径：固定报表 → 国库收支存明细表
iframe:   fineReportTsasRpt1010
导出流程：点击「导出」→「Excel」→「原样导出」

自动生成任务：起止月份 × 国库列表 × 辖属标志（全辖/本级）

依赖：pip install playwright pandas openpyxl psutil
      playwright install chromium
"""

import sys
import os
import re
import threading
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox
import pandas as pd
from openpyxl import load_workbook
from datetime import datetime, date
import time
import traceback
import asyncio

from playwright.async_api import async_playwright, Page

try:
    import psutil
except ImportError:
    psutil = None

try:
    import pyperclip
except ImportError:
    pyperclip = None


# ============================================================================
# 常量
# ============================================================================
IFRAME_NAME  = "fineReportTsasRpt1010"
MENU_PATH    = ["固定报表", "国库收支存明细表"]

# 辖属标志映射：列名 → (下拉选项前缀, 文件名后缀)
GOVERN_MAP = {
    "下载全辖": ("0", "全辖"),
    "下载本级": ("1", "本级"),
}

# 全局停止标志
stop_flag    = False
sniper_flag  = False


# ============================================================================
# 帮助：生成月份序列
# ============================================================================
def _months_between(start: str, end: str):
    """
    生成 start 到 end 之间的所有月份字符串（YYYYMM）。
    start/end 可为 YYYYMM 或 YYYY/MM 格式。
    """
    def _parse(s):
        s = s.strip().replace("/", "").replace("-", "")
        if len(s) == 6:
            return int(s[:4]), int(s[4:6])
        raise ValueError(f"无法解析月份：{s}")

    sy, sm = _parse(start)
    ey, em = _parse(end)
    result = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        result.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return result


# ============================================================================
# 主应用类
# ============================================================================
class GDMXApp(tk.Tk):
    """国库收支存明细表批量下载工具"""

    BG_PRIMARY   = "#1a1a2e"
    BG_CARD      = "#16213e"
    BG_INPUT     = "#0f3460"
    BG_LOG       = "#0d1117"
    FG_PRIMARY   = "#e8e8e8"
    FG_SECONDARY = "#8892a8"
    FG_ACCENT    = "#00d2ff"
    CLR_GREEN    = "#00e676"
    CLR_RED      = "#ff5252"
    CLR_YELLOW   = "#ffd740"
    CLR_BLUE     = "#448aff"
    CLR_HOVER_G  = "#00c853"
    CLR_HOVER_R  = "#ff1744"
    CLR_HOVER_B  = "#2979ff"

    def __init__(self):
        super().__init__()
        self.title("国库收支存明细表  批量下载工具")
        self.geometry("980x820")
        self.resizable(True, True)
        self.configure(bg=self.BG_PRIMARY)
        self.minsize(800, 650)

        self.excel_path        = tk.StringVar()
        self.download_folder   = tk.StringVar()
        self.start_month_var   = tk.StringVar(value="202401")
        self.end_month_var     = tk.StringVar(value="202512")
        self.start_year_var    = tk.StringVar(value="2024")
        self.end_year_var      = tk.StringVar(value="2025")
        self.enable_year_var   = tk.BooleanVar(value=True)
        self.chrome_path_var   = tk.StringVar()
        self.target_url        = None
        self.is_running        = False
        self.worker_thread     = None
        self.sniper_thread     = None
        self.current_nav_done  = False

        self._init_styles()
        self._build_gui()

    # ------------------------------------------------------------------ styles
    def _init_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Card.TFrame",  background=self.BG_CARD)
        style.configure("Main.TFrame",  background=self.BG_PRIMARY)
        style.configure("Title.TLabel", background=self.BG_PRIMARY,
                        foreground=self.FG_ACCENT,  font=("Segoe UI", 16, "bold"))
        style.configure("Sub.TLabel",   background=self.BG_PRIMARY,
                        foreground=self.FG_SECONDARY, font=("Segoe UI", 9))
        style.configure("Card.TLabel",  background=self.BG_CARD,
                        foreground=self.FG_SECONDARY, font=("Segoe UI", 10))
        style.configure("LogTitle.TLabel", background=self.BG_PRIMARY,
                        foreground=self.FG_SECONDARY, font=("Segoe UI", 9, "bold"))
        style.configure("Path.TEntry",
                        fieldbackground=self.BG_INPUT, foreground=self.FG_PRIMARY,
                        borderwidth=0, padding=(8, 6))
        style.map("Path.TEntry",
                  fieldbackground=[("readonly", self.BG_INPUT)],
                  foreground=[("readonly", self.FG_PRIMARY)])

    def _make_btn(self, parent, text, command, color, hover, width=16, state=tk.NORMAL):
        btn = tk.Button(
            parent, text=text, command=command,
            bg=color, fg="white", activebackground=hover, activeforeground="white",
            font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2",
            bd=0, padx=14, pady=8, width=width, state=state,
            highlightthickness=0
        )
        btn.bind("<Enter>", lambda e: btn.config(bg=hover)  if btn["state"] != "disabled" else None)
        btn.bind("<Leave>", lambda e: btn.config(bg=color)  if btn["state"] != "disabled" else None)
        btn._base_color  = color
        btn._hover_color = hover
        return btn

    # ------------------------------------------------------------------ GUI
    def _build_gui(self):
        c = ttk.Frame(self, style="Main.TFrame", padding=(24, 16))
        c.pack(fill=tk.BOTH, expand=True)

        # 标题
        hdr = ttk.Frame(c, style="Main.TFrame")
        hdr.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(hdr, text="国库收支存明细表  批量下载", style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Label(hdr, text="固定报表 · 月报+年报 · 全辖/本级按国库配置",
                  style="Sub.TLabel").pack(side=tk.LEFT, padx=(14, 0), pady=(8, 0))

        # 文件卡片
        card = ttk.Frame(c, style="Card.TFrame", padding=(20, 16))
        card.pack(fill=tk.X, pady=(0, 10))

        self._file_row(card, "国库列表文件", self.excel_path, self._select_excel, row_pady=(0, 10))
        self._file_row(card, "保存目录    ", self.download_folder, self._select_folder)

        # 月报行
        row3 = ttk.Frame(card, style="Card.TFrame")
        row3.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(row3, text="月报起始", style="Card.TLabel", width=10).pack(side=tk.LEFT)
        tk.Entry(row3, textvariable=self.start_month_var,
                 bg=self.BG_INPUT, fg=self.FG_PRIMARY, insertbackground=self.FG_ACCENT,
                 font=("Segoe UI", 10), relief="flat", bd=0, highlightthickness=0, width=12
                 ).pack(side=tk.LEFT, padx=(8, 20))
        ttk.Label(row3, text="月报终止", style="Card.TLabel", width=10).pack(side=tk.LEFT)
        tk.Entry(row3, textvariable=self.end_month_var,
                 bg=self.BG_INPUT, fg=self.FG_PRIMARY, insertbackground=self.FG_ACCENT,
                 font=("Segoe UI", 10), relief="flat", bd=0, highlightthickness=0, width=12
                 ).pack(side=tk.LEFT, padx=(8, 10))
        ttk.Label(row3, text="格式 YYYYMM，如 202401", style="Card.TLabel").pack(side=tk.LEFT, padx=(10, 0))

        # 年报行
        row_yr = ttk.Frame(card, style="Card.TFrame")
        row_yr.pack(fill=tk.X, pady=(8, 0))
        tk.Checkbutton(row_yr, text="下载年报", variable=self.enable_year_var,
                       bg=self.BG_CARD, fg=self.FG_PRIMARY, selectcolor=self.BG_INPUT,
                       activebackground=self.BG_CARD, activeforeground=self.FG_PRIMARY,
                       font=("Segoe UI", 10), bd=0, highlightthickness=0
                       ).pack(side=tk.LEFT)
        ttk.Label(row_yr, text="起始年", style="Card.TLabel", width=6).pack(side=tk.LEFT, padx=(12, 0))
        tk.Entry(row_yr, textvariable=self.start_year_var,
                 bg=self.BG_INPUT, fg=self.FG_PRIMARY, insertbackground=self.FG_ACCENT,
                 font=("Segoe UI", 10), relief="flat", bd=0, highlightthickness=0, width=8
                 ).pack(side=tk.LEFT, padx=(6, 16))
        ttk.Label(row_yr, text="终止年", style="Card.TLabel", width=6).pack(side=tk.LEFT)
        tk.Entry(row_yr, textvariable=self.end_year_var,
                 bg=self.BG_INPUT, fg=self.FG_PRIMARY, insertbackground=self.FG_ACCENT,
                 font=("Segoe UI", 10), relief="flat", bd=0, highlightthickness=0, width=8
                 ).pack(side=tk.LEFT, padx=(6, 10))
        ttk.Label(row_yr, text="格式 YYYY，报表类型=年", style="Card.TLabel").pack(side=tk.LEFT, padx=(10, 0))

        # Chrome 行
        row4 = ttk.Frame(card, style="Card.TFrame")
        row4.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(row4, text="Chrome路径", style="Card.TLabel", width=10).pack(side=tk.LEFT)
        tk.Entry(row4, textvariable=self.chrome_path_var,
                 bg=self.BG_INPUT, fg=self.FG_PRIMARY, insertbackground=self.FG_ACCENT,
                 font=("Segoe UI", 10), relief="flat", bd=0, highlightthickness=0
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 10))
        ttk.Label(row4, text="（空=自动检测）", style="Card.TLabel").pack(side=tk.LEFT)

        # 按钮栏
        btn_bar = ttk.Frame(c, style="Main.TFrame")
        btn_bar.pack(fill=tk.X, pady=(4, 10))

        self.start_btn = self._make_btn(btn_bar, "  开始批量下载  ", self._start_task,
                                        self.CLR_GREEN, self.CLR_HOVER_G)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.stop_btn = self._make_btn(btn_bar, "  停止  ", self._stop_task,
                                       self.CLR_RED, self.CLR_HOVER_R, width=8, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 8))

        self._make_btn(btn_bar, "  生成国库模板  ", self._gen_template,
                       self.CLR_BLUE, self.CLR_HOVER_B, width=14).pack(side=tk.LEFT, padx=(0, 8))

        self.sniper_btn = self._make_btn(btn_bar, "  捕获登录链接  ", self._start_sniper,
                                         self.CLR_BLUE, self.CLR_HOVER_B, width=16)
        self.sniper_btn.pack(side=tk.RIGHT)

        # 日志
        ttk.Label(c, text="CONSOLE", style="LogTitle.TLabel").pack(anchor=tk.W, pady=(0, 4))
        lf = tk.Frame(c, bg=self.BG_LOG, bd=0, highlightthickness=1, highlightbackground="#2a2a4a")
        lf.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(lf, bg=self.BG_LOG, fg="#8cc8a8",
                                font=("Cascadia Code", 10), wrap=tk.WORD,
                                insertbackground=self.FG_ACCENT, selectbackground="#264f78",
                                relief="flat", bd=0, padx=12, pady=10, highlightthickness=0)
        sb = ttk.Scrollbar(lf, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.log_text.tag_config("INFO",    foreground="#8cc8a8")
        self.log_text.tag_config("SUCCESS", foreground=self.CLR_GREEN)
        self.log_text.tag_config("ERROR",   foreground=self.CLR_RED)
        self.log_text.tag_config("WARN",    foreground=self.CLR_YELLOW)
        self.log_text.tag_config("TS",      foreground="#5a6a7a")
        self.log_text.config(state=tk.DISABLED)

    def _file_row(self, parent, label, var, cmd, row_pady=(0, 0)):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill=tk.X, pady=row_pady)
        ttk.Label(row, text=label, style="Card.TLabel", width=10).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=var, style="Path.TEntry", state="readonly"
                  ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 10))
        self._make_btn(row, "选择", cmd, self.BG_INPUT, "#1a5276", width=8).pack(side=tk.LEFT)

    # ------------------------------------------------------------------ actions
    def _select_excel(self):
        p = filedialog.askopenfilename(title="选择国库列表Excel文件",
                                       filetypes=[("Excel文件", "*.xlsx *.xls")])
        if p:
            self.excel_path.set(p)
            self.log(f"已选择国库列表文件: {p}")

    def _select_folder(self):
        p = filedialog.askdirectory(title="选择保存目录")
        if p:
            self.download_folder.set(p)
            self.log(f"已选择保存目录: {p}")

    def _gen_template(self):
        """生成国库列表模板 Excel"""
        p = filedialog.asksaveasfilename(
            title="保存国库列表模板", defaultextension=".xlsx",
            initialfile="国库列表模板.xlsx",
            filetypes=[("Excel文件", "*.xlsx")]
        )
        if not p:
            return
        df = pd.DataFrame({
            "国库代码": ["101001", "101002", "101003"],
            "国库名称": ["XX市国库", "XX区国库", "XX县国库"],
        })
        df.to_excel(p, index=False, sheet_name="国库列表")
        self.log(f"模板已生成: {p}  请填入真实国库代码后再使用", "SUCCESS")

    def log(self, msg: str, level: str = "INFO"):
        def _append():
            self.log_text.config(state=tk.NORMAL)
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_text.insert(tk.END, f"[{ts}]", "TS")
            self.log_text.insert(tk.END, f" {msg}\n", level)
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.after(0, _append)

    # ------------------------------------------------------------------ sniper
    def _start_sniper(self):
        global sniper_flag
        if psutil is None:
            messagebox.showerror("缺少依赖", "请先安装 psutil: pip install psutil")
            return
        if not self.excel_path.get() or not os.path.exists(self.excel_path.get()):
            messagebox.showwarning("提示", "请先选择国库列表Excel文件")
            return
        if not self.download_folder.get() or not os.path.isdir(self.download_folder.get()):
            messagebox.showwarning("提示", "请先选择保存目录")
            return
        sniper_flag = False
        self.target_url = None
        self.sniper_btn.config(state=tk.DISABLED, text="  正在监控...  ", bg="#5c6bc0")
        self.log("URL 狙击手已启动，请在客户端触发系统登录...", "WARN")
        self.sniper_thread = threading.Thread(target=self._sniper_worker, daemon=True)
        self.sniper_thread.start()

    def _sniper_worker(self):
        global sniper_flag
        browser_names = {"chrome.exe", "msedge.exe", "firefox.exe",
                         "chrome", "msedge", "firefox"}
        known_pids = set()
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if (proc.info["name"] or "").lower() in browser_names:
                    known_pids.add(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        captured_url = None
        start_time = time.time()
        while not sniper_flag and (time.time() - start_time) < 180:
            try:
                for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                    try:
                        pname = (proc.info["name"] or "").lower()
                        if pname not in browser_names or proc.info["pid"] in known_pids:
                            continue
                        for arg in (proc.info["cmdline"] or []):
                            if isinstance(arg, str) and arg.startswith("http"):
                                captured_url = arg
                                try:
                                    proc.kill()
                                except Exception:
                                    pass
                                break
                        if captured_url:
                            break
                        known_pids.add(proc.info["pid"])
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                if captured_url:
                    break
            except Exception as e:
                self.log(f"狙击手异常: {e}", "ERROR")
            time.sleep(0.01)

        if captured_url:
            self.target_url = captured_url
            self.log("Token 链接捕获成功！", "SUCCESS")
            if pyperclip:
                try:
                    pyperclip.copy(captured_url)
                except Exception:
                    pass
            self.after(0, lambda: self.sniper_btn.config(
                state=tk.NORMAL, text="  捕获登录链接  ", bg=self.CLR_BLUE))
            self.after(500, self._start_task)
        else:
            self.log("狙击手超时（180秒），未捕获到链接", "WARN")
            self.after(0, lambda: self.sniper_btn.config(
                state=tk.NORMAL, text="  捕获登录链接  ", bg=self.CLR_BLUE))

    # ------------------------------------------------------------------ task control
    def _start_task(self):
        global stop_flag
        if not self.excel_path.get() or not os.path.exists(self.excel_path.get()):
            messagebox.showerror("错误", "请选择有效的国库列表Excel文件")
            return
        if not self.download_folder.get() or not os.path.isdir(self.download_folder.get()):
            messagebox.showerror("错误", "请选择有效的保存目录")
            return
        if not self.target_url:
            messagebox.showwarning("提示",
                "尚未捕获到登录链接。\n请先点击「捕获登录链接」，然后在客户端触发系统登录。")
            return
        stop_flag = False
        self.current_nav_done = False
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.is_running = True
        self.log("=" * 50, "SUCCESS")
        self.log("批量下载任务开始", "SUCCESS")
        self.log("=" * 50, "SUCCESS")
        self.worker_thread = threading.Thread(
            target=self._worker,
            args=(self.excel_path.get(), self.download_folder.get()),
            daemon=True
        )
        self.worker_thread.start()

    def _stop_task(self):
        global stop_flag
        stop_flag = True
        self.log("用户请求停止...", "WARN")

    def _worker(self, excel_file, dl_folder):
        try:
            asyncio.run(self._run_automation(excel_file, dl_folder))
        except Exception as e:
            self.log(f"任务出错: {e}", "ERROR")
            self.log(traceback.format_exc(), "ERROR")
        finally:
            self.is_running = False
            self.after(0, lambda: self.start_btn.config(state=tk.NORMAL))
            self.after(0, lambda: self.stop_btn.config(state=tk.DISABLED))
            self.log("=" * 50, "SUCCESS")
            self.log("全部任务执行完毕", "SUCCESS")
            self.log("=" * 50, "SUCCESS")

    # ================================================================
    # 核心自动化
    # ================================================================
    async def _run_automation(self, excel_file: str, dl_folder: str):
        global stop_flag

        # 1. 读取国库列表（含辖属配置）
        self.log("读取国库列表...")
        try:
            df = pd.read_excel(excel_file, dtype=str).fillna("")
            code_col = next((c for c in df.columns
                             if "代码" in c or "code" in c.lower()), df.columns[0])
            name_col = next((c for c in df.columns
                             if "名称" in c or "name" in c.lower()),
                            df.columns[1] if len(df.columns) > 1 else df.columns[0])
            # 每行读取：国库代码、名称、辖属选项列表
            treasuries = []
            for _, row in df.iterrows():
                code = str(row[code_col]).strip()
                if not code:
                    continue
                name = str(row[name_col]).strip()
                # 读取「下载全辖」「下载本级」列，默认全辖+本级都下
                gov_opts = []
                for col_name, (gov_code, gov_label) in GOVERN_MAP.items():
                    if col_name in df.columns:
                        val = str(row[col_name]).strip().upper()
                        if val in ("是", "Y", "YES", "TRUE", "1", "√", "✓"):
                            gov_opts.append((gov_code, gov_label))
                    # 若列不存在，默认下载全辖+本级
                if not gov_opts:
                    gov_opts = [("0", "全辖"), ("1", "本级")]
                treasuries.append((code, name, gov_opts))
        except Exception as e:
            self.log(f"读取Excel失败: {e}", "ERROR")
            return

        if not treasuries:
            self.log("未读取到有效国库数据", "ERROR")
            return
        self.log(f"已读取 {len(treasuries)} 个国库", "SUCCESS")

        # 2. 生成月份序列
        try:
            months = _months_between(self.start_month_var.get(), self.end_month_var.get())
        except Exception as e:
            self.log(f"月份解析失败: {e}", "ERROR")
            return
        self.log(f"月报范围: {months[0]} ~ {months[-1]}，共 {len(months)} 个月")

        # 3. 生成年份列表
        years = []
        if self.enable_year_var.get():
            try:
                sy = int(self.start_year_var.get().strip())
                ey = int(self.end_year_var.get().strip())
                years = [str(y) for y in range(sy, ey + 1)]
                self.log(f"年报范围: {years[0]} ~ {years[-1]}，共 {len(years)} 年")
            except Exception as e:
                self.log(f"年份解析失败: {e}，跳过年报", "WARN")

        # 4. 构建任务列表：月报 + 年报
        tasks = []
        # 月报：月份 × 国库 × 辖属
        for month in months:
            for tre_code, tre_name, gov_opts in treasuries:
                for gov_code, gov_label in gov_opts:
                    tasks.append({
                        "rpt_type": "3",          # 3 -- 月
                        "date":     month,        # YYYYMM
                        "tre_code": tre_code,
                        "tre_name": tre_name,
                        "gov_code": gov_code,
                        "gov_label": gov_label,
                    })
        # 年报：年份 × 国库 × 辖属
        for year in years:
            for tre_code, tre_name, gov_opts in treasuries:
                for gov_code, gov_label in gov_opts:
                    tasks.append({
                        "rpt_type": "5",          # 5 -- 年
                        "date":     year,         # YYYY
                        "tre_code": tre_code,
                        "tre_name": tre_name,
                        "gov_code": gov_code,
                        "gov_label": gov_label,
                    })

        total = len(tasks)
        month_cnt = len(months) * sum(len(t[2]) for t in treasuries)
        year_cnt  = len(years)  * sum(len(t[2]) for t in treasuries)
        self.log(f"共生成 {total} 个任务（月报 {month_cnt} + 年报 {year_cnt}）", "SUCCESS")

        # 4. 启动浏览器
        self.log("启动 Chrome 浏览器...")
        async with async_playwright() as pw:
            try:
                launch_kwargs = {
                    "headless": False,
                    "channel": "chrome",
                    "args": [
                        "--ignore-certificate-errors",
                        "--ignore-ssl-errors",
                        "--disable-web-security",
                        "--no-first-run",
                        "--disable-popup-blocking",
                        "--start-maximized",
                    ]
                }
                chrome_path = self.chrome_path_var.get().strip()
                if chrome_path and os.path.exists(chrome_path):
                    launch_kwargs.pop("channel", None)
                    launch_kwargs["executable_path"] = chrome_path
                browser = await pw.chromium.launch(**launch_kwargs)
            except Exception as e:
                self.log(f"启动浏览器失败: {e}", "ERROR")
                return

            context = await browser.new_context(
                ignore_https_errors=True,
                viewport=None,
                accept_downloads=True,
            )
            page = await context.new_page()

            self.log("携 Token 登录内网系统...")
            try:
                await page.goto(self.target_url, wait_until="networkidle", timeout=60000)
                self.log("登录成功", "SUCCESS")
            except Exception as e:
                self.log(f"页面加载提示: {e}", "WARN")

            await asyncio.sleep(3)

            # 5. 循环执行任务
            success_count = fail_count = 0

            for idx, task in enumerate(tasks):
                if stop_flag:
                    self.log("已停止", "WARN")
                    break

                rpt_type  = task["rpt_type"]
                date      = task["date"]
                tre_code  = task["tre_code"]
                tre_name  = task["tre_name"]
                gov_code  = task["gov_code"]
                gov_label = task["gov_label"]
                # 年报文件名加「年」后缀区分
                rpt_label  = "月报" if rpt_type == "3" else "年报"
                date_label = f"{date}年" if rpt_type == "5" else date
                fname      = f"固定收支存_{tre_code}_{gov_label}_{date_label}"

                self.log(f"\n{'─'*45}")
                self.log(f"▶ [{idx+1}/{total}] {fname}（{rpt_label}）")
                self.log(f"{'─'*45}")

                try:
                    # 首次或菜单未打开时导航
                    if not self.current_nav_done:
                        await self._navigate_to_report(page)
                        self.current_nav_done = True
                    else:
                        self.log("  复用已打开的报表页面", "INFO")

                    await asyncio.sleep(1.5)
                    await self._fill_form(page, date, tre_code, gov_code, rpt_type)
                    await self._click_query_and_wait(page)
                    saved_path = await self._export_and_save(page, dl_folder, fname)

                    success_count += 1
                    self.log(f"  已保存: {os.path.basename(saved_path)}", "SUCCESS")

                except Exception as e:
                    fail_count += 1
                    self.log(f"  任务失败: {e}", "ERROR")
                    self.log(traceback.format_exc(), "ERROR")
                    # 失败后重置导航状态，下一条任务重新导航
                    self.current_nav_done = False

                await asyncio.sleep(1.5)

            self.log(f"\n汇总: 共{total}条，成功{success_count}，失败{fail_count}",
                     "SUCCESS" if fail_count == 0 else "WARN")

            try:
                await context.close()
                await browser.close()
            except Exception:
                pass

    # ================================================================
    # 导航
    # ================================================================
    async def _navigate_to_report(self, page: Page):
        """点击侧边栏菜单：固定报表 → 国库收支存明细表"""
        self.log(f"  导航到菜单: {' → '.join(MENU_PATH)}")

        for i, item_text in enumerate(MENU_PATH):
            target = page.get_by_text(item_text, exact=True).first
            # 如果是父级菜单，检查子项是否已展开
            if i < len(MENU_PATH) - 1:
                next_item = page.get_by_text(MENU_PATH[i + 1], exact=True).first
                try:
                    if await next_item.is_visible(timeout=1000):
                        self.log(f"    子菜单已展开，跳过: {item_text}")
                        continue
                except Exception:
                    pass

            try:
                await target.wait_for(state="visible", timeout=10000)
                await target.click()
                await asyncio.sleep(1)
            except Exception:
                self.log(f"    补偿点击: {item_text}", "WARN")
                try:
                    await target.click(force=True)
                    await asyncio.sleep(1)
                except Exception as e2:
                    self.log(f"    菜单点击失败: {item_text} - {e2}", "ERROR")
                    raise

        self.log("  导航完成", "SUCCESS")

    # ================================================================
    # 表单填充
    # ================================================================
    async def _fill_form(self, page: Page, date: str, tre_code: str,
                         gov_code: str, rpt_type: str = "3"):
        """
        填写表单字段。
        rpt_type="3"（3 -- 月）：date 为 YYYYMM
        rpt_type="5"（5 -- 年）：date 为 YYYY
        """
        rpt_label = "月报" if rpt_type == "3" else "年报"
        self.log(f"  填充表单（{rpt_label}，日期={date}）...")

        # 金额单位 → 0 -- 元
        await self._select_dropdown(page, "pAmtUnit", "0")

        # 报表类型 → 3 -- 月 / 5 -- 年
        await self._select_dropdown(page, "pRptType", rpt_type)

        # 日期（月报=YYYY/MM，年报=YYYY）
        await self._fill_date(page, "pDate", date, rpt_type)

        # 辖属区标志
        await self._select_dropdown(page, "pGovernFlag", gov_code)

        # 国库选择
        await self._fill_treasury(page, "pTreCode", tre_code)

        self.log("  表单填充完成", "SUCCESS")

    async def _select_dropdown(self, page: Page, field_id: str, value: str):
        """Element-UI 下拉菜单选择（容器定位法）"""
        self.log(f"    下拉 {field_id} = {value}")
        form_item = page.locator(f'.el-form-item:has(label[for="{field_id}"])').first
        inp = form_item.locator('.el-select .el-input__inner').first
        await inp.wait_for(state="visible", timeout=10000)
        await inp.click()
        await asyncio.sleep(0.6)

        found = False
        # Element-UI 下拉弹出层在全局 body 下，需用 page.locator
        # 等待下拉列表出现
        try:
            await page.wait_for_selector(
                ".el-select-dropdown:not([style*='display: none']) .el-select-dropdown__item",
                timeout=3000
            )
        except Exception:
            pass

        items = page.locator(
            ".el-select-dropdown:not([style*='display: none']) .el-select-dropdown__item"
        )
        count = await items.count()
        for i in range(count):
            item = items.nth(i)
            text = (await item.text_content() or "").strip()
            # 匹配策略：完整相等、以"值 "开头（如"0 -- 元"）、值在文本中
            if (text == value
                    or text.startswith(f"{value} ")
                    or text.startswith(f"{value}--")
                    or f"-- {value}" in text
                    or value in text):
                await item.click()
                found = True
                break

        if not found:
            # 回退：尝试 :has-text 选择器
            try:
                candidate = page.locator(
                    f".el-select-dropdown:not([style*='display: none']) "
                    f".el-select-dropdown__item:has-text('{value}')"
                ).first
                if await candidate.is_visible(timeout=1000):
                    await candidate.click()
                    found = True
            except Exception:
                pass

        if not found:
            self.log(f"    下拉选项 '{value}' 未找到，跳过", "WARN")
            await page.keyboard.press("Escape")

        await asyncio.sleep(0.3)

    async def _fill_date(self, page: Page, field_id: str, date: str, rpt_type: str = "3"):
        """
        填充日期选择器。
        rpt_type="3"（月报）：date=YYYYMM，转为 YYYY/MM 输入
        rpt_type="5"（年报）：date=YYYY，直接输入年份
        """
        self.log(f"    日期 {field_id} = {date}（{'月报' if rpt_type=='3' else '年报'}）")
        if rpt_type == "3":
            display_val = f"{date[:4]}/{date[4:]}"   # YYYY/MM
        else:
            display_val = date                        # YYYY

        form_item = page.locator(f'.el-form-item:has(label[for="{field_id}"])').first
        inp = form_item.locator('.el-date-editor .el-input__inner').first
        await inp.wait_for(state="visible", timeout=10000)

        await inp.triple_click()
        await asyncio.sleep(0.2)
        await inp.fill("")
        await asyncio.sleep(0.1)
        await inp.type(display_val, delay=60)
        await asyncio.sleep(0.4)
        await inp.press("Enter")
        await asyncio.sleep(0.5)
        # 关闭可能残留的日历弹层
        try:
            picker = page.locator(".el-month-table, .el-year-table, .el-picker-panel")
            if await picker.first.is_visible(timeout=500):
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.2)
        except Exception:
            pass

    async def _fill_treasury(self, page: Page, field_id: str, tre_code: str):
        """在国库选择输入框中直接键入国库代码，并触发 Vue 数据绑定"""
        self.log(f"    国库 {field_id} = {tre_code}")
        form_item = page.locator(f'.el-form-item:has(label[for="{field_id}"])').first
        inp = form_item.locator('.el-input__inner').first
        await inp.wait_for(state="visible", timeout=10000)

        await inp.triple_click()          # 全选已有内容
        await asyncio.sleep(0.15)
        await inp.fill("")                # 清空
        await asyncio.sleep(0.1)
        await inp.type(tre_code, delay=40)
        await asyncio.sleep(0.3)
        # 触发 input/change 事件让 Vue 更新数据绑定
        await inp.dispatch_event("input")
        await asyncio.sleep(0.1)
        await inp.dispatch_event("change")
        await asyncio.sleep(0.1)
        await inp.press("Enter")
        await asyncio.sleep(0.4)
        # Tab 跳出字段，确保 blur 事件触发
        await inp.press("Tab")
        await asyncio.sleep(0.3)

    # ================================================================
    # 查询等待
    # ================================================================
    async def _click_query_and_wait(self, page: Page):
        """点击查询，等待 iframe 内报表渲染完成"""
        self.log("  点击查询...")
        query_btn = page.locator("button.el-button:has-text('查询')").first
        await query_btn.wait_for(state="visible", timeout=5000)
        await query_btn.click()

        self.log("  等待报表加载（最长 6 分钟）...")
        await asyncio.sleep(3)

        iframe = page.frame_locator(f'iframe[name="{IFRAME_NAME}"]')
        # 以「导出」按钮（button.fr-btn-text.x-emb-export）的父级 .fr-btn 是否含
        # ui-state-enabled 作为报表加载完成的信号
        max_wait, poll, elapsed = 360, 2, 0
        export_ready = False
        while elapsed < max_wait:
            try:
                export_btn_parent = iframe.locator(
                    '.fr-btn:has(button.x-emb-export)'
                ).first
                cls = await export_btn_parent.get_attribute("class", timeout=1500)
                if cls and "ui-state-enabled" in cls:
                    export_ready = True
            except Exception:
                pass

            if export_ready:
                break
            await asyncio.sleep(poll)
            elapsed += poll
            if elapsed % 30 == 0:
                self.log(f"  仍在等待... ({elapsed}s)", "INFO")

        if export_ready:
            self.log("  报表加载完成", "SUCCESS")
        else:
            self.log("  等待超时，继续尝试导出...", "WARN")
        await asyncio.sleep(1)

    # ================================================================
    # 导出：导出 → Excel → 原样导出
    # ================================================================
    async def _export_and_save(self, page: Page, dl_folder: str, fname: str) -> str:
        """
        在帆软 iframe 内执行三步导出：
        1. 点击工具栏「导出」按钮
        2. 点击弹出菜单中的「Excel」
        3. 点击子菜单中的「原样导出」
        同时用 expect_download 拦截实际下载。
        """
        self.log("  准备导出...")
        iframe = page.frame_locator(f'iframe[name="{IFRAME_NAME}"]')

        async with page.expect_download(timeout=360000) as dl_info:
            # ----------------------------------------------------------------
            # 步骤 1：点击工具栏「导出」按钮
            # 实际 HTML：<div class="fr-btn ui-state-enabled">
            #               <button class="fr-btn-text x-emb-export">导出</button>
            #            </div>
            # 注意：该 div 没有 widgetname 属性，用内部 button class 定位
            # ----------------------------------------------------------------
            export_btn = iframe.locator('button.fr-btn-text.x-emb-export').first
            await export_btn.wait_for(state="visible", timeout=15000)
            self.log("  点击导出按钮...")
            await export_btn.click()
            await asyncio.sleep(0.8)

            # ----------------------------------------------------------------
            # 步骤 2：点击一级菜单「Excel」
            # 实际 HTML：<div class="fr-ui-core-menu menu">
            #               <div class="menu-item"><div class="menu-text">Excel</div></div>
            #            </div>
            # 菜单在 iframe 内渲染，用 iframe.locator
            # ----------------------------------------------------------------
            excel_item = iframe.locator(
                '.fr-ui-core-menu.menu .menu-item:has(.menu-text:text("Excel"))'
            ).first
            await excel_item.wait_for(state="visible", timeout=5000)
            self.log("  点击 Excel 菜单项...")
            await excel_item.hover()      # hover 触发子菜单展开
            await asyncio.sleep(0.5)

            # ----------------------------------------------------------------
            # 步骤 3：点击二级菜单「原样导出」
            # 实际 HTML（初始 display:none，hover Excel 后显示）：
            #   <div class="fr-ui-core-menu menu">
            #     <div class="menu-item"><div class="menu-text">原样导出</div></div>
            #   </div>
            # ----------------------------------------------------------------
            raw_item = iframe.locator(
                '.fr-ui-core-menu.menu .menu-item:has(.menu-text:text("原样导出"))'
            ).first
            await raw_item.wait_for(state="visible", timeout=5000)
            self.log("  点击原样导出...")
            await raw_item.click()

        download = await dl_info.value
        # 取原始文件扩展名（xlsx / xls），忽略系统生成的文件名主体
        orig = download.suggested_filename or "report.xlsx"
        ext = os.path.splitext(orig)[1] or ".xlsx"
        safe_fname = re.sub(r'[\\/:*?"<>|]', '', fname)
        save_path = os.path.join(dl_folder, f"{safe_fname}{ext}")
        await download.save_as(save_path)
        return save_path


# ============================================================================
# 入口
# ============================================================================
def main():
    app = GDMXApp()
    app.log("国库收支存明细表  批量下载工具  就绪", "SUCCESS")
    app.log("")
    app.log("使用步骤：")
    app.log("  1. 点击「生成国库模板」→ 填入14个国库代码与名称 → 保存")
    app.log("  2. 选择填好的国库列表文件，选择保存目录")
    app.log("  3. 确认起止月份（默认 202401 ~ 202512）")
    app.log("  4. 点击「捕获登录链接」，在客户端触发系统登录")
    app.log("  5. 程序自动导航到「固定报表→国库收支存明细表」，批量执行全部任务")
    app.log("")
    app.log(f"  任务量预估：24个月 × 14库 × 2辖属 = 672次下载", "WARN")
    try:
        app.mainloop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
