#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TMIS数据自由查询批量抓取工具 v5.1
===========================
使用 Playwright 自动启动系统 Chrome 浏览器，携 Token 登录内网系统，
自动填表、查询、导出报表，并可选数据后处理。

功能：
1. URL 狙击手：捕获 Token 链接 + 自动启动浏览器执行任务
2. 自动检测系统 Chrome（channel="chrome"），支持手动指定路径
3. 智能/自定义命名（参数表"文件名称"列 或 自动命名）
4. 收入/支出/退库/库存业务分流（侧边栏动态导航、字段复用映射）
5. 可选数据后处理（openpyxl 保留原始格式）
6. UI 日期选择器（默认当日，Excel参数优先）
7. 查询成功精确检测（等待"原样导出"按钮可用）
8. 智能标签页管理（类型切换时自动关闭旧标签页，防止 DOM 冲突）

依赖：pip install playwright pandas openpyxl psutil
      pip install pyperclip  (可选)
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
from datetime import datetime
import time
import traceback
import asyncio

# Playwright
from playwright.async_api import async_playwright, Page

# URL 狙击手所需
try:
    import psutil
except ImportError:
    psutil = None

try:
    import pyperclip
except ImportError:
    pyperclip = None


# ============================================================================
# 全局停止标志
# ============================================================================
stop_flag = False          # 停止批量任务
sniper_flag = False        # 停止URL狙击手

# ============================================================================
# Excel 中文表头映射词典
# ============================================================================
COLUMN_MAPPING = {
    "报表库选择": "pRptDbType",
    "调整期标志": "pTrimFlag",
    "报表类型": "pRptType",
    "起始日期": "pStartDate",
    "终止日期": "pEndDate",
    "国库选择": "pTreCode",
    "辖属标志": "pGovernFlag",
    "预算级次": "pBdgLevel",
    "征收机关性质": "pTaxOrgProp",
    "预算科目": "pSbtCode",
    "科目级次": "pStatSbtLevel",
    "金额单位": "pAmtUnit",
    "分国库查询": "pByTre",  # 兼容旧模板
    "预算单位": "pBdgOrgCode",
    "银行大类": "pBnkClass",
    "计划单列市/县/乡": "pSigTreArea",
    "展示范围": "pShowScope",
    "退库原因": "pDwbkReason",
    "国库属性": "pTreAttrib",
    "会计科目": "pBookSbt",
    "会计账户": "pBookAcctName",
    "会计账户名称": "pBookAcctName",
}


# ============================================================================
# 自由查询页面配置
# ============================================================================
REPORT_CONFIGS = {
    "收入": {
        "menu_title": "收入数据自由查询",
        "iframe_name": "fineReportTsasRpt6010",
        "dropdown_fields": [
            "pRptDbType", "pTrimFlag", "pRptType", "pGovernFlag",
            "pTaxOrgProp", "pSigTreArea", "pStatSbtLevel", "pShowScope", "pAmtUnit",
        ],
        "date_fields": ["pStartDate", "pEndDate"],
        "text_fields": ["pTreCode", "pBdgLevel", "pSbtCode"],
        "checkbox_fields": ["分序时", "分地区", "分预算级次", "分预算科目", "分征收机关", "是否展示同比"],
        "template_columns": [
            "文件名称", "是否追加日期", "保留原文件名",
            "报表库选择", "调整期标志", "报表类型", "起始日期", "终止日期",
            "国库选择", "辖属标志", "预算级次", "征收机关性质", "预算科目",
            "计划单列市/县/乡", "科目级次", "展示范围", "金额单位",
            "分序时", "分地区", "分预算级次", "分预算科目", "分征收机关", "是否展示同比",
        ],
        "sample": {
            "文件名称": "收入自由查询", "是否追加日期": "1", "保留原文件名": "1",
            "报表库选择": "1 -- 报表库", "调整期标志": "0 -- 正常期", "报表类型": "",
            "起始日期": "202601", "终止日期": "202601",
            "国库选择": "", "辖属标志": "0 -- 全辖", "预算级次": "",
            "征收机关性质": "0000000000 -- 不分征收机关", "预算科目": "",
            "计划单列市/县/乡": "0 -- 不含", "科目级次": "4 -- 目",
            "展示范围": "1 -- 下级", "金额单位": "2 -- 万元",
            "分序时": "1", "分地区": "0", "分预算级次": "1",
            "分预算科目": "1", "分征收机关": "0", "是否展示同比": "0",
        },
    },
    "支出": {
        "menu_title": "支出数据自由查询",
        "iframe_name": "fineReportTsasRpt6020",
        "dropdown_fields": [
            "pRptDbType", "pTrimFlag", "pRptType", "pGovernFlag",
            "pBdgOrgCode", "pBnkClass", "pSigTreArea", "pStatSbtLevel", "pShowScope", "pAmtUnit",
        ],
        "date_fields": ["pStartDate", "pEndDate"],
        "text_fields": ["pTreCode", "pBdgLevel", "pSbtCode"],
        "checkbox_fields": [
            "分序时", "分地区", "分预算单位", "分银行大类",
            "分预算级次", "分预算科目", "是否展示同比",
        ],
        "template_columns": [
            "文件名称", "是否追加日期", "保留原文件名",
            "报表库选择", "调整期标志", "报表类型", "起始日期", "终止日期",
            "国库选择", "辖属标志", "预算单位", "银行大类", "预算级次", "预算科目",
            "计划单列市/县/乡", "科目级次", "展示范围", "金额单位",
            "分序时", "分地区", "分预算单位", "分银行大类",
            "分预算级次", "分预算科目", "是否展示同比",
        ],
        "sample": {
            "文件名称": "支出自由查询", "是否追加日期": "1", "保留原文件名": "1",
            "报表库选择": "1 -- 报表库", "调整期标志": "0 -- 正常期", "报表类型": "",
            "起始日期": "202601", "终止日期": "202601",
            "国库选择": "", "辖属标志": "0 -- 全辖",
            "预算单位": "ALL -- ALL", "银行大类": "0 -- 全部",
            "预算级次": "", "预算科目": "",
            "计划单列市/县/乡": "0 -- 不含", "科目级次": "4 -- 目",
            "展示范围": "1 -- 下级", "金额单位": "2 -- 万元",
            "分序时": "1", "分地区": "0", "分预算单位": "0", "分银行大类": "0",
            "分预算级次": "1", "分预算科目": "1", "是否展示同比": "0",
        },
    },
    "退库": {
        "menu_title": "退库数据自由查询",
        "iframe_name": "fineReportTsasRpt6030",
        "dropdown_fields": [
            "pRptDbType", "pTrimFlag", "pRptType", "pGovernFlag",
            "pTaxOrgProp", "pSigTreArea", "pStatSbtLevel", "pAmtUnit",
        ],
        "date_fields": ["pStartDate", "pEndDate"],
        "text_fields": ["pTreCode", "pBdgLevel", "pSbtCode", "pDwbkReason"],
        "checkbox_fields": [
            "分序时", "分地区", "分征收机关", "分预算级次",
            "分退库原因", "分预算科目", "是否展示同比",
        ],
        "template_columns": [
            "文件名称", "是否追加日期", "保留原文件名",
            "报表库选择", "调整期标志", "报表类型", "起始日期", "终止日期",
            "国库选择", "辖属标志", "征收机关性质", "预算级次", "预算科目", "退库原因",
            "计划单列市/县/乡", "科目级次", "金额单位",
            "分序时", "分地区", "分征收机关", "分预算级次",
            "分退库原因", "分预算科目", "是否展示同比",
        ],
        "sample": {
            "文件名称": "退库自由查询", "是否追加日期": "1", "保留原文件名": "1",
            "报表库选择": "1 -- 报表库", "调整期标志": "0 -- 正常期", "报表类型": "",
            "起始日期": "202601", "终止日期": "202601",
            "国库选择": "", "辖属标志": "0 -- 全辖",
            "征收机关性质": "0000000000 -- 不分征收机关",
            "预算级次": "", "预算科目": "", "退库原因": "",
            "计划单列市/县/乡": "0 -- 不含", "科目级次": "4 -- 目", "金额单位": "2 -- 万元",
            "分序时": "1", "分地区": "0", "分征收机关": "0", "分预算级次": "1",
            "分退库原因": "1", "分预算科目": "1", "是否展示同比": "0",
        },
    },
    "库存": {
        "menu_title": "库存数据自由查询",
        "iframe_name": "fineReportTsasRpt6040",
        "dropdown_fields": [
            "pRptDbType", "pRptType", "pGovernFlag", "pTreAttrib",
            "pSigTreArea", "pShowScope", "pAmtUnit",
        ],
        "date_fields": ["pStartDate", "pEndDate"],
        "text_fields": ["pTreCode", "pBdgLevel", "pBookSbt", "pBookAcctName"],
        "checkbox_fields": [
            "分序时", "分地区", "分国库属性", "分预算级次",
            "分会计科目", "分会计账户", "是否展示同比",
        ],
        "template_columns": [
            "文件名称", "是否追加日期", "保留原文件名",
            "报表库选择", "报表类型", "起始日期", "终止日期",
            "国库选择", "辖属标志", "国库属性", "预算级次",
            "会计科目", "会计账户名称", "计划单列市/县/乡",
            "展示范围", "金额单位",
            "分序时", "分地区", "分国库属性", "分预算级次",
            "分会计科目", "分会计账户", "是否展示同比",
        ],
        "sample": {
            "文件名称": "库存自由查询", "是否追加日期": "1", "保留原文件名": "1",
            "报表库选择": "1 -- 报表库", "报表类型": "3 -- 月",
            "起始日期": "202601", "终止日期": "202603",
            "国库选择": "", "辖属标志": "0 -- 全辖",
            "国库属性": "0 -- 全部", "预算级次": "0",
            "会计科目": "271,272,273", "会计账户名称": "ALL",
            "计划单列市/县/乡": "0 -- 不含", "展示范围": "1 -- 下级", "金额单位": "0 -- 元",
            "分序时": "1", "分地区": "0", "分国库属性": "0", "分预算级次": "1",
            "分会计科目": "1", "分会计账户": "1", "是否展示同比": "0",
        },
    },
}


# ============================================================================
# 主应用类
# ============================================================================
class TMISAutoApp(tk.Tk):
    """TMIS数据批量抓取工具主窗口"""

    # ====================================================================
    # 设计系统：颜色常量
    # ====================================================================
    # 深色主题配色
    BG_PRIMARY   = "#1a1a2e"    # 主背景（深靛蓝）
    BG_CARD      = "#16213e"    # 卡片背景
    BG_INPUT     = "#0f3460"    # 输入框背景
    BG_LOG       = "#0d1117"    # 日志区背景（近纯黑）
    FG_PRIMARY   = "#e8e8e8"    # 主文字
    FG_SECONDARY = "#8892a8"    # 次要文字
    FG_ACCENT    = "#00d2ff"    # 强调色（青蓝）
    CLR_GREEN    = "#00e676"    # 成功绿
    CLR_RED      = "#ff5252"    # 错误红
    CLR_YELLOW   = "#ffd740"    # 警告黄
    CLR_BLUE     = "#448aff"    # 按钮蓝
    CLR_HOVER_G  = "#00c853"    # 绿按钮悬浮
    CLR_HOVER_R  = "#ff1744"    # 红按钮悬浮
    CLR_HOVER_B  = "#2979ff"    # 蓝按钮悬浮

    def __init__(self):
        super().__init__()
        self.title("TMIS  数据自由查询批量抓取工具 v5.1")
        self.geometry("980x820")
        self.resizable(True, True)
        self.configure(bg=self.BG_PRIMARY)
        self.minsize(800, 650)

        # 初始化变量
        self.excel_path = tk.StringVar()
        self.download_folder = tk.StringVar()
        self.worker_thread = None
        self.sniper_thread = None
        self.is_running = False

        # URL 狙击手捕获到的登录链接
        self.target_url = None

        # 日期变量（默认当天）
        today = datetime.now()
        self.start_date_var = tk.StringVar(value=today.strftime("%Y%m%d"))
        self.end_date_var = tk.StringVar(value=today.strftime("%Y%m%d"))

        # 数据后处理开关
        self.enable_postprocess = tk.BooleanVar(value=False)

        # 命名模式
        self.naming_mode = tk.StringVar(value="param")

        # Chrome 路径（空=自动检测）
        self.chrome_path_var = tk.StringVar()

        # 记录当前导航到的数据类型
        self.current_nav_type = None

        # 构建 GUI
        self._init_styles()
        self._build_gui()

    # ========================================================================
    # 样式初始化
    # ========================================================================
    def _init_styles(self):
        """配置 ttk 自定义主题样式"""
        style = ttk.Style(self)
        style.theme_use("clam")

        # ---- 全局 Frame ----
        style.configure("Card.TFrame", background=self.BG_CARD)
        style.configure("Main.TFrame", background=self.BG_PRIMARY)

        # ---- Label ----
        style.configure("Title.TLabel",
                        background=self.BG_PRIMARY, foreground=self.FG_ACCENT,
                        font=("Segoe UI", 18, "bold"))
        style.configure("Subtitle.TLabel",
                        background=self.BG_PRIMARY, foreground=self.FG_SECONDARY,
                        font=("Segoe UI", 9))
        style.configure("Card.TLabel",
                        background=self.BG_CARD, foreground=self.FG_SECONDARY,
                        font=("Segoe UI", 10))
        style.configure("LogTitle.TLabel",
                        background=self.BG_PRIMARY, foreground=self.FG_SECONDARY,
                        font=("Segoe UI", 9, "bold"))

        # ---- Entry (路径显示) ----
        style.configure("Path.TEntry",
                        fieldbackground=self.BG_INPUT, foreground=self.FG_PRIMARY,
                        borderwidth=0, padding=(8, 6))
        style.map("Path.TEntry",
                  fieldbackground=[("readonly", self.BG_INPUT)],
                  foreground=[("readonly", self.FG_PRIMARY)])

        # ---- Separator ----
        style.configure("Gray.TSeparator", background="#2a2a4a")

    def _make_btn(self, parent, text, command, color, hover_color, width=18, state=tk.NORMAL):
        """创建自定义扁平化按钮（tk.Button，支持悬浮变色）"""
        btn = tk.Button(
            parent, text=text, command=command,
            bg=color, fg="white", activebackground=hover_color, activeforeground="white",
            font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2",
            bd=0, padx=16, pady=8, width=width, state=state,
            highlightthickness=0
        )
        # 悬浮效果
        btn.bind("<Enter>", lambda e: btn.config(bg=hover_color) if btn["state"] != "disabled" else None)
        btn.bind("<Leave>", lambda e: btn.config(bg=color) if btn["state"] != "disabled" else None)
        # 存储原始颜色以便恢复
        btn._base_color = color
        btn._hover_color = hover_color
        return btn

    # ========================================================================
    # GUI 构建
    # ========================================================================
    def _build_gui(self):
        """构建现代化深色主题界面"""

        # 外层容器
        container = ttk.Frame(self, style="Main.TFrame", padding=(24, 16))
        container.pack(fill=tk.BOTH, expand=True)

        # ============ 标题区 ============
        header = ttk.Frame(container, style="Main.TFrame")
        header.pack(fill=tk.X, pady=(0, 12))

        ttk.Label(header, text="TMIS  数据自由查询批量抓取工具",
                  style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Label(header, text="v5.1   |   收入 / 支出 / 退库 / 库存 自动填表 / 查询 / 导出 / 清洗",
                  style="Subtitle.TLabel").pack(side=tk.LEFT, padx=(16, 0), pady=(8, 0))

        # ============ 文件选择卡片 ============
        card = ttk.Frame(container, style="Card.TFrame", padding=(20, 16))
        card.pack(fill=tk.X, pady=(0, 10))

        # --- Excel 文件行 ---
        row1 = ttk.Frame(card, style="Card.TFrame")
        row1.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(row1, text="参数文件", style="Card.TLabel", width=10).pack(side=tk.LEFT)
        excel_entry = ttk.Entry(row1, textvariable=self.excel_path,
                                style="Path.TEntry", state="readonly")
        excel_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 10))
        self._make_btn(row1, "选择", self._select_excel,
                       self.BG_INPUT, "#1a5276", width=8).pack(side=tk.LEFT)

        # --- 保存文件夹行 ---
        row2 = ttk.Frame(card, style="Card.TFrame")
        row2.pack(fill=tk.X)

        ttk.Label(row2, text="保存目录", style="Card.TLabel", width=10).pack(side=tk.LEFT)
        folder_entry = ttk.Entry(row2, textvariable=self.download_folder,
                                 style="Path.TEntry", state="readonly")
        folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 10))
        self._make_btn(row2, "选择", self._select_folder,
                       self.BG_INPUT, "#1a5276", width=8).pack(side=tk.LEFT)

        # --- 日期行 ---
        row3 = ttk.Frame(card, style="Card.TFrame")
        row3.pack(fill=tk.X, pady=(10, 0))

        ttk.Label(row3, text="起始日期", style="Card.TLabel", width=10).pack(side=tk.LEFT)
        tk.Entry(row3, textvariable=self.start_date_var,
                 bg=self.BG_INPUT, fg=self.FG_PRIMARY, insertbackground=self.FG_ACCENT,
                 font=("Segoe UI", 10), relief="flat", bd=0, highlightthickness=0, width=14
                 ).pack(side=tk.LEFT, padx=(8, 20))

        ttk.Label(row3, text="终止日期", style="Card.TLabel", width=10).pack(side=tk.LEFT)
        tk.Entry(row3, textvariable=self.end_date_var,
                 bg=self.BG_INPUT, fg=self.FG_PRIMARY, insertbackground=self.FG_ACCENT,
                 font=("Segoe UI", 10), relief="flat", bd=0, highlightthickness=0, width=14
                 ).pack(side=tk.LEFT, padx=(8, 10))

        ttk.Label(row3, text="(YYYY/YYYYMM/YYYYMMDD，Excel优先)",
                  style="Card.TLabel").pack(side=tk.LEFT, padx=(10, 0))

        # --- Chrome 路径行 ---
        row4 = ttk.Frame(card, style="Card.TFrame")
        row4.pack(fill=tk.X, pady=(10, 0))

        ttk.Label(row4, text="Chrome路径", style="Card.TLabel", width=10).pack(side=tk.LEFT)
        tk.Entry(row4, textvariable=self.chrome_path_var,
                 bg=self.BG_INPUT, fg=self.FG_PRIMARY, insertbackground=self.FG_ACCENT,
                 font=("Segoe UI", 10), relief="flat", bd=0, highlightthickness=0
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 10))
        ttk.Label(row4, text="(空=自动检测)", style="Card.TLabel").pack(side=tk.LEFT)

        # ============ 选项卡片 ============
        opt_card = ttk.Frame(container, style="Card.TFrame", padding=(20, 10))
        opt_card.pack(fill=tk.X, pady=(0, 10))

        tk.Checkbutton(
            opt_card, text="启用数据后处理（新增预算级次列+剔除计划单列市）",
            variable=self.enable_postprocess,
            bg=self.BG_CARD, fg=self.FG_SECONDARY, activebackground=self.BG_CARD,
            activeforeground=self.FG_PRIMARY, selectcolor=self.BG_INPUT,
            font=("Segoe UI", 10), highlightthickness=0, bd=0
        ).pack(side=tk.LEFT, padx=(0, 30))

        ttk.Label(opt_card, text="命名:", style="Card.TLabel").pack(side=tk.LEFT, padx=(0, 6))
        for txt, val in [(" 参数表命名", "param"), ("自动命名", "auto")]:
            tk.Radiobutton(
                opt_card, text=txt, variable=self.naming_mode, value=val,
                bg=self.BG_CARD, fg=self.FG_SECONDARY, activebackground=self.BG_CARD,
                activeforeground=self.FG_PRIMARY, selectcolor=self.BG_INPUT,
                font=("Segoe UI", 10), highlightthickness=0, bd=0
            ).pack(side=tk.LEFT, padx=(0, 10))

        # ============ 操作按钮栏 ============
        btn_bar = ttk.Frame(container, style="Main.TFrame")
        btn_bar.pack(fill=tk.X, pady=(4, 10))

        self.start_btn = self._make_btn(
            btn_bar, "  开始批量执行  ", self._start_task,
            self.CLR_GREEN, self.CLR_HOVER_G, width=16
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.stop_btn = self._make_btn(
            btn_bar, "  停止  ", self._stop_task,
            self.CLR_RED, self.CLR_HOVER_R, width=8, state=tk.DISABLED
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.sniper_btn = self._make_btn(
            btn_bar, "  捕获登录链接  ", self._start_sniper,
            self.CLR_BLUE, self.CLR_HOVER_B, width=16
        )
        self.sniper_btn.pack(side=tk.RIGHT)

        # ============ 日志区域 ============
        log_header = ttk.Frame(container, style="Main.TFrame")
        log_header.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(log_header, text="CONSOLE", style="LogTitle.TLabel").pack(side=tk.LEFT)

        # 日志文本框
        log_frame = tk.Frame(container, bg=self.BG_LOG, bd=0, highlightthickness=1,
                             highlightbackground="#2a2a4a")
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(
            log_frame, bg=self.BG_LOG, fg="#8cc8a8",
            font=("Cascadia Code", 10), wrap=tk.WORD,
            insertbackground=self.FG_ACCENT, selectbackground="#264f78",
            relief="flat", bd=0, padx=12, pady=10,
            highlightthickness=0
        )

        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 日志颜色标签
        self.log_text.tag_config("INFO",    foreground="#8cc8a8")   # 柔绿
        self.log_text.tag_config("SUCCESS", foreground=self.CLR_GREEN)
        self.log_text.tag_config("ERROR",   foreground=self.CLR_RED)
        self.log_text.tag_config("WARN",    foreground=self.CLR_YELLOW)
        self.log_text.tag_config("TS",      foreground="#5a6a7a")   # 时间戳灰

        # 禁止用户编辑日志区（但允许选中复制）
        self.log_text.config(state=tk.DISABLED)

    # ========================================================================
    # 用户交互方法
    # ========================================================================
    def _select_excel(self):
        path = filedialog.askopenfilename(
            title="选择Excel参数文件",
            filetypes=[("Excel文件", "*.xlsx *.xls"), ("所有文件", "*.*")]
        )
        if path:
            self.excel_path.set(path)
            self.log(f"已选择参数文件: {path}")

    def _select_folder(self):
        path = filedialog.askdirectory(title="选择下载保存文件夹")
        if path:
            self.download_folder.set(path)
            self.log(f"已选择保存文件夹: {path}")

    def log(self, msg: str, level: str = "INFO"):
        """线程安全的日志输出"""
        def _append():
            self.log_text.config(state=tk.NORMAL)
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_text.insert(tk.END, f"[{ts}]", "TS")
            self.log_text.insert(tk.END, f" {msg}\n", level)
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.after(0, _append)

    # ========================================================================
    # 需求1：URL 狙击手 —— 捕获 Token 链接 + 全自动启动
    # ========================================================================
    def _start_sniper(self):
        """启动 URL 狙击手守护线程"""
        global sniper_flag

        if psutil is None:
            messagebox.showerror("缺少依赖", "请先安装 psutil: pip install psutil")
            return

        # 预检：Excel 和文件夹必须已选好（捕获后会直接开始任务）
        if not self.excel_path.get() or not os.path.exists(self.excel_path.get()):
            messagebox.showwarning("提示", "请先选择 Excel 参数文件，捕获成功后将自动开始任务")
            return
        if not self.download_folder.get() or not os.path.isdir(self.download_folder.get()):
            messagebox.showwarning("提示", "请先选择保存目录，捕获成功后将自动开始任务")
            return

        sniper_flag = False
        self.target_url = None
        self.sniper_btn.config(state=tk.DISABLED, text="  正在监控...  ", bg="#5c6bc0")
        self.log("URL 狙击手已启动 (10ms 极速轮询)", "WARN")
        self.log("请在客户端触发系统登录，本工具将自动拦截 Token 链接", "WARN")

        self.sniper_thread = threading.Thread(target=self._sniper_worker, daemon=True)
        self.sniper_thread.start()

    def _sniper_worker(self):
        """
        URL 狙击手核心逻辑：
        以 10ms 极限间隔高频轮询新浏览器进程，从 cmdline 中提取 http 链接，
        立即 kill 进程（在请求发出前截断），将链接保存到 self.target_url，
        然后自动触发批量任务执行。
        """
        global sniper_flag

        # 记录启动前已有的浏览器进程 PID，避免误杀
        browser_names = {"chrome.exe", "msedge.exe", "firefox.exe", "iexplore.exe",
                         "chrome", "msedge", "firefox"}
        known_pids = set()
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if proc.info["name"] and proc.info["name"].lower() in browser_names:
                    known_pids.add(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        self.log(f"已标记 {len(known_pids)} 个已有浏览器进程，开始监控...", "INFO")

        captured_url = None
        max_wait = 180  # 最多等待3分钟
        start_time = time.time()

        while not sniper_flag and (time.time() - start_time) < max_wait:
            try:
                for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                    try:
                        pname = (proc.info["name"] or "").lower()
                        if pname not in browser_names:
                            continue
                        if proc.info["pid"] in known_pids:
                            continue

                        # 发现新浏览器进程！扫描命令行参数
                        cmdline = proc.info["cmdline"] or []
                        for arg in cmdline:
                            if isinstance(arg, str) and arg.startswith("http"):
                                captured_url = arg
                                # 立即击杀！在请求发出前掐断进程
                                try:
                                    proc.kill()
                                    self.log(f"已击杀浏览器进程 PID={proc.info['pid']}", "SUCCESS")
                                except Exception as kill_err:
                                    self.log(f"击杀进程失败: {kill_err}", "ERROR")
                                break

                        if captured_url:
                            break

                        known_pids.add(proc.info["pid"])

                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue

                if captured_url:
                    break

            except Exception as e:
                self.log(f"狙击手监控异常: {e}", "ERROR")

            time.sleep(0.01)  # 10ms 极限轮询间隔

        # ---- 处理结果 ----
        if captured_url:
            self.target_url = captured_url
            self.log("Token 链接捕获成功!", "SUCCESS")
            # 只显示前100个字符，避免日志过长
            display_url = captured_url if len(captured_url) <= 100 else captured_url[:100] + "..."
            self.log(f"链接: {display_url}", "SUCCESS")

            # 同时复制到剪贴板（备用）
            try:
                if pyperclip:
                    pyperclip.copy(captured_url)
                    self.log("链接已同步复制到剪贴板（备用）", "INFO")
            except Exception:
                pass

            self.log("即将自动启动 Playwright 浏览器并执行全部任务...", "SUCCESS")

            # 恢复按钮状态，然后自动触发任务执行
            self.after(0, lambda: self.sniper_btn.config(
                state=tk.NORMAL, text="  捕获登录链接  ", bg=self.CLR_BLUE
            ))
            # 延迟500ms后自动开始任务，让UI有时间更新
            self.after(500, self._start_task)

        else:
            self.log("狙击手监控超时（180秒），未捕获到链接", "WARN")
            self.after(0, lambda: self.sniper_btn.config(
                state=tk.NORMAL, text="  捕获登录链接  ", bg=self.CLR_BLUE
            ))

    # ========================================================================
    # 批量任务启动与停止
    # ========================================================================
    def _start_task(self):
        """启动批量执行"""
        global stop_flag

        excel_file = self.excel_path.get()
        dl_folder = self.download_folder.get()

        if not excel_file or not os.path.exists(excel_file):
            messagebox.showerror("错误", "请选择有效的Excel参数文件")
            return
        if not dl_folder or not os.path.isdir(dl_folder):
            messagebox.showerror("错误", "请选择有效的下载保存文件夹")
            return

        # 必须先捕获到 Token 链接
        if not self.target_url:
            messagebox.showwarning("提示",
                "尚未捕获到登录链接。\n\n"
                "请先点击「捕获登录链接」，然后在客户端触发系统登录。")
            return

        stop_flag = False
        self.current_nav_type = None  # 重置导航状态
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.is_running = True

        self.log("=" * 50, "SUCCESS")
        self.log("批量任务开始执行", "SUCCESS")
        self.log("=" * 50, "SUCCESS")

        self.worker_thread = threading.Thread(
            target=self._worker, args=(excel_file, dl_folder), daemon=True
        )
        self.worker_thread.start()

    def _stop_task(self):
        """停止任务"""
        global stop_flag
        stop_flag = True
        self.log("用户请求停止任务...", "WARN")

    def _worker(self, excel_file: str, dl_folder: str):
        """后台工作线程"""
        try:
            asyncio.run(self._run_automation(excel_file, dl_folder))
        except Exception as e:
            self.log(f"任务执行出错: {e}", "ERROR")
            self.log(traceback.format_exc(), "ERROR")
        finally:
            self.is_running = False
            self.after(0, lambda: self.start_btn.config(state=tk.NORMAL))
            self.after(0, lambda: self.stop_btn.config(state=tk.DISABLED))
            self.log("=" * 50, "SUCCESS")
            self.log("全部任务执行完毕", "SUCCESS")
            self.log("=" * 50, "SUCCESS")

    # ========================================================================
    # 核心自动化逻辑（异步）
    # ========================================================================
    async def _run_automation(self, excel_file: str, dl_folder: str):
        """主自动化流程（全新浏览器实例 + Token URL 直达）"""
        global stop_flag

        # ---- 1. 读取 Excel ----
        self.log("正在读取Excel参数文件...")
        try:
            # 读取所有 sheet
            sheet_dict = pd.read_excel(excel_file, sheet_name=None, dtype=str)
            tasks = []
            
            for sheet_name, df_sheet in sheet_dict.items():
                current_sheet_sz_type = None
                for report_type in REPORT_CONFIGS:
                    if report_type in sheet_name:
                        current_sheet_sz_type = report_type
                        break
                else:
                    # 尝试从历史“收支类型/数据类型”列获取，兼容老模板
                    if "收支类型" not in df_sheet.columns and "数据类型" not in df_sheet.columns:
                        continue
                
                df_sheet = df_sheet.fillna("")
                
                # 表头映射替换 (将中文列名映射为内部 pXXX 变量名)
                new_columns = {}
                for col in df_sheet.columns:
                    col_str = str(col).strip()
                    if col_str in COLUMN_MAPPING:
                        new_columns[col_str] = COLUMN_MAPPING[col_str]
                
                if new_columns:
                    df_sheet.rename(columns=new_columns, inplace=True)
                
                for _, row in df_sheet.iterrows():
                    # 如果行内本身有“收支类型/数据类型”则优先（兼容老模板）
                    row_sz = str(row.get("收支类型", row.get("数据类型", ""))).strip()
                    if row_sz in REPORT_CONFIGS:
                        current_sz_type = row_sz
                    else:
                        current_sz_type = current_sheet_sz_type if current_sheet_sz_type else "收入"

                    if current_sz_type not in REPORT_CONFIGS:
                        self.log(f"跳过暂不支持的任务类型: {current_sz_type}", "WARN")
                        continue
                        
                    tasks.append((current_sz_type, row))
                    
            if not tasks:
                self.log("未在Excel中找到有效的任务数据 (请检查Sheet名称是否包含'收入'/'支出'/'退库'/'库存')", "ERROR")
                return
                
            total = len(tasks)
            self.log(f"成功读取 {total} 条任务", "SUCCESS")
        except Exception as e:
            self.log(f"读取Excel失败: {e}", "ERROR")
            return

        # ---- 2. 启动全新浏览器并携 Token 登录 ----
        self.log("正在启动系统 Chrome 浏览器 (有头模式)...")
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
                    self.log(f"使用手动指定的 Chrome: {chrome_path}")
                else:
                    self.log("自动检测系统 Chrome (channel=chrome)")
                browser = await pw.chromium.launch(**launch_kwargs)
            except Exception as e:
                self.log(f"启动浏览器失败: {e}", "ERROR")
                self.log("请确保已安装 Playwright 浏览器: playwright install chromium", "WARN")
                return

            self.log("浏览器启动成功", "SUCCESS")

            # 创建上下文和页面
            context = await browser.new_context(
                ignore_https_errors=True,   # 上下文级别也忽略 HTTPS 错误
                viewport=None,              # 使用浏览器窗口实际大小
                accept_downloads=True,      # 允许文件下载
            )
            page = await context.new_page()

            # 携 Token 直达内网主页
            self.log(f"正在携 Token 访问内网系统...")
            try:
                await page.goto(self.target_url, wait_until="networkidle", timeout=60000)
                self.log("内网系统登录成功!", "SUCCESS")
            except Exception as e:
                # 有些内网页面 networkidle 可能超时但实际已加载
                self.log(f"页面加载提示: {e}", "WARN")
                self.log("继续执行...", "INFO")

            # 等待页面完全稳定
            await asyncio.sleep(3)

            # ---- 3. 循环处理每一行任务 ----
            success_count = 0
            fail_count = 0
            name_counter = {}

            for idx, (sz_type, row) in enumerate(tasks):
                if stop_flag:
                    self.log("用户已停止任务", "WARN")
                    break

                dynamic_name = self._build_filename(row, sz_type, name_counter)

                self.log(f"\n{'─' * 45}")
                self.log(f"▶ 第 {idx + 1}/{total} 条任务 [{sz_type}]: {dynamic_name}")
                self.log(f"{'─' * 45}")

                try:
                    await self._navigate_sidebar_smart(page, sz_type)

                    await asyncio.sleep(2)

                    await self._fill_form(page, row, sz_type)

                    await self._click_query_and_wait(page, sz_type)

                    keep_original_name = self._should_keep_original_name(row)
                    saved_path = await self._export_and_save(
                        page, dl_folder, dynamic_name, sz_type, keep_original_name
                    )

                    if self.enable_postprocess.get() and saved_path and os.path.exists(saved_path):
                        self._process_excel_data(saved_path, row, sz_type)
                    elif not self.enable_postprocess.get():
                        self.log("  数据后处理已关闭，跳过", "INFO")

                    success_count += 1
                    self.log(f"任务完成: {dynamic_name}", "SUCCESS")

                except Exception as e:
                    fail_count += 1
                    self.log(f"任务失败: {e}", "ERROR")
                    self.log(traceback.format_exc(), "ERROR")

                await asyncio.sleep(2)

            # ---- 4. 汇总 ----
            self.log(f"\n执行汇总: 共 {total} 条, 成功 {success_count} 条, 失败 {fail_count} 条",
                     "SUCCESS" if fail_count == 0 else "WARN")

            # ---- 5. 清理浏览器 ----
            self.log("正在关闭浏览器...")
            try:
                await context.close()
                await browser.close()
            except Exception:
                pass

    # ========================================================================
    # 智能/自定义命名
    # ========================================================================
    def _build_filename(self, row: pd.Series, sz_type: str, name_counter: dict = None) -> str:
        """根据命名模式生成文件名前缀。"""
        if name_counter is None:
            name_counter = {}

        start_date = str(row.get("pStartDate", "")).strip()
        end_date = str(row.get("pEndDate", "")).strip()
        if not start_date:
            start_date = self.start_date_var.get().strip()
        if not end_date:
            end_date = self.end_date_var.get().strip()

        # 模式1：参数表命名
        if self.naming_mode.get() == "param":
            custom_name = str(row.get("文件名称", "")).strip()
            if custom_name:
                append_date = str(row.get("是否追加日期", "1")).strip().lower()
                if append_date in ("0", "否", "false", "no", "n"):
                    return re.sub(r'[\\/:*?"<>|]', '', custom_name)
                if start_date == end_date or not end_date:
                    date_part = start_date if start_date else ""
                else:
                    date_part = f"{start_date}至{end_date}"
                name = f"{custom_name}_{date_part}" if date_part else custom_name
                return re.sub(r'[\\/:*?"<>|]', '', name)
            else:
                self.log("    参数表未填写'文件名称'，回退到自动命名", "WARN")

        # 模式2：自动命名
        if start_date == end_date or not end_date:
            date_part = start_date if start_date else "未知日期"
        else:
            date_part = f"{start_date}至{end_date}"

        bdg_level = str(row.get("pBdgLevel", "")).strip()
        bdg_code = bdg_level.split("--")[0].strip() if "--" in bdg_level else bdg_level
        scale_part = "全口径" if bdg_code in ("", "0") else "地方"

        by_tre = str(row.get("pByTre", "")).strip()
        if "--" in by_tre:
            tre_name = by_tre.split("--")[-1].strip()
        elif "—" in by_tre:
            tre_name = by_tre.split("—")[-1].strip()
        else:
            chinese_chars = re.findall(r'[\u4e00-\u9fff]+', by_tre)
            tre_name = "".join(chinese_chars) if chinese_chars else by_tre
        if not tre_name:
            tre_code = str(row.get("pTreCode", "")).strip()
            tre_name = tre_code if tre_code else "全国"

        name = f"{date_part}_{scale_part}_{sz_type}_{tre_name}"
        safe_name = re.sub(r'[\\/:*?"<>|]', '', name)

        if safe_name in name_counter:
            name_counter[safe_name] += 1
            safe_name = f"{safe_name}_{name_counter[safe_name]}"
        else:
            name_counter[safe_name] = 0

        return safe_name

    def _should_keep_original_name(self, row: pd.Series) -> bool:
        """是否在自定义文件名前继续拼接系统原始导出文件名。"""
        keep = str(row.get("保留原文件名", "1")).strip().lower()
        return keep not in ("0", "否", "false", "no", "n")

    # ========================================================================
    # 需求4：智能侧边栏操作与标签页管理
    # ========================================================================
    async def _close_current_tab(self, page: Page, sz_type: str):
        """关闭当前的查询标签页，防止多标签页 DOM 冲突"""
        tab_name = REPORT_CONFIGS.get(sz_type, {}).get("menu_title", f"{sz_type}数据自由查询")
        self.log(f"  尝试关闭标签页: {tab_name}...", "INFO")
        try:
            # 找到包含对应文本的 tab 节点，然后再点它里面的 .el-icon-close
            close_btn = page.locator(f"span.tags-view-item:has-text('{tab_name}')").locator(".el-icon-close").first
            if await close_btn.is_visible(timeout=3000):
                await close_btn.click()
                self.log(f"  标签页已关闭", "SUCCESS")
                self.current_nav_type = None  # 重置当前导航状态，强制下一次必须点侧边栏
            else:
                self.log(f"  标签页未显示关闭按钮", "WARN")
        except Exception as e:
            self.log(f"  关闭标签页失败: {e}", "WARN")

    async def _navigate_sidebar_smart(self, page: Page, sz_type: str):
        """
        智能导航侧边栏。根据数据类型动态拼接目标菜单。
        如果当前已经导航到同一类型，跳过。

        导航路径：固定报表 -> 数据自由查询 -> {收入/支出/退库/库存}数据自由查询
        """
        config = REPORT_CONFIGS[sz_type]
        menu_title = config["menu_title"]

        if self.current_nav_type and self.current_nav_type != sz_type:
            self.log(f"  检测到数据类型将切换（{self.current_nav_type} -> {sz_type}），正在关闭旧标签页...", "WARN")
            await self._close_current_tab(page, self.current_nav_type)

        if self.current_nav_type == sz_type:
            self.log(f"  当前已在「{menu_title}」页面，跳过导航", "INFO")
            return

        self.log(f"  导航到「{menu_title}」...")

        # 完整菜单路径
        menu_items = [
            "固定报表",
            "数据自由查询",
            menu_title,
        ]

        for item_text in menu_items:
            self.log(f"    点击菜单: {item_text}")

            # 智能容错：先检查子菜单是否已可见
            target = page.get_by_text(item_text, exact=True).first
            try:
                is_vis = await target.is_visible()
            except Exception:
                is_vis = False

            if item_text in ("固定报表", "数据自由查询"):
                # 对于父级菜单，如果目标子菜单已经可见，可跳过点击
                # 检查下一级菜单是否已可见
                next_idx = menu_items.index(item_text) + 1
                if next_idx < len(menu_items):
                    next_item = page.get_by_text(menu_items[next_idx], exact=True).first
                    try:
                        next_vis = await next_item.is_visible()
                        if next_vis:
                            self.log(f"    子菜单已展开，跳过点击: {item_text}")
                            continue
                    except Exception:
                        pass

            # 等待并点击
            try:
                await target.wait_for(state="visible", timeout=10000)
                await target.click()
                await asyncio.sleep(1)
            except Exception:
                # 二次补偿点击
                self.log(f"    首次点击未响应，执行二次补偿点击: {item_text}", "WARN")
                await asyncio.sleep(0.5)
                try:
                    await target.click(force=True)
                    await asyncio.sleep(1)
                except Exception as e2:
                    self.log(f"    菜单点击失败: {item_text} - {e2}", "ERROR")
                    raise

        self.current_nav_type = sz_type
        self.log(f"  导航完成: {menu_title}", "SUCCESS")

    # ========================================================================
    # 需求4：表单填充（支持收入/支出分流）
    # ========================================================================
    async def _fill_form(self, page: Page, row: pd.Series, sz_type: str):
        """
        填充表单，根据数据类型分流处理字段。

        Args:
            page: Playwright Page 对象
            row: Excel 数据行
            sz_type: "收入"、"支出"、"退库" 或 "库存"
        """
        self.log("  开始填充表单...")
        config = REPORT_CONFIGS[sz_type]

        # ---- 下拉选择字段 ----
        for field_id in config["dropdown_fields"]:
            val = str(row.get(field_id, "")).strip()
            if not val:
                continue
            await self._fill_dropdown(page, field_id, val)

        # ---- 日期字段（Excel 优先，UI 后备） ----
        ui_dates = {
            "pStartDate": self.start_date_var.get().strip(),
            "pEndDate": self.end_date_var.get().strip(),
        }
        for field_id in config["date_fields"]:
            val = str(row.get(field_id, "")).strip()
            if not val:
                val = ui_dates.get(field_id, "")
            if not val:
                continue
            await self._fill_date(page, field_id, val)

        # ---- 文本输入字段 ----
        for field_id in config["text_fields"]:
            val = str(row.get(field_id, "")).strip()
            if not val:
                continue
            await self._fill_text_input(page, field_id, val)

        # ---- 复选框（根据数据类型分流） ----
        await self._fill_checkboxes(page, row, sz_type)

        self.log("  表单填充完成", "SUCCESS")

    async def _fill_checkboxes(self, page: Page, row: pd.Series, sz_type: str):
        """
        处理复选框。
        根据当前自由查询页面配置，直接读取对应中文列名的 0/1 值。
        """
        for cb_name in REPORT_CONFIGS[sz_type]["checkbox_fields"]:
            val = str(row.get(cb_name, "")).strip()
            if val:
                await self._set_checkbox(page, cb_name, int(val))


    # ========================================================================
    # 表单字段操作方法
    # ========================================================================
    async def _fill_date(self, page: Page, field_id: str, value: str):
        """填充日期字段（el-date-editor）"""
        self.log(f"    日期 {field_id} = {value}")

        # 精确定位：先找到包含该 label 的 form-item 容器，再在容器内找日期输入框
        form_item = page.locator(f'.el-form-item:visible:has(label[for="{field_id}"])').first
        inp = form_item.locator('.el-date-editor .el-input__inner').first
        await inp.wait_for(state="visible", timeout=10000)

        await inp.click()
        await asyncio.sleep(0.3)
        await inp.press("Control+a")
        await asyncio.sleep(0.1)
        await inp.press("Delete")
        await asyncio.sleep(0.1)
        await inp.type(value, delay=50)
        await asyncio.sleep(0.2)
        await inp.press("Enter")
        await asyncio.sleep(0.5)

    async def _fill_text_input(self, page: Page, field_id: str, value: str):
        """填充文本输入框，填入后 Enter + Tab 触发 Vue 绑定"""
        self.log(f"    文本 {field_id} = {value}")

        # 精确定位：先找到包含该 label 的 form-item 容器，再在容器内找文本输入框
        form_item = page.locator(f'.el-form-item:visible:has(label[for="{field_id}"])').first
        inp = form_item.locator('.el-input .el-input__inner').first
        await inp.wait_for(state="visible", timeout=10000)

        await inp.click()
        await asyncio.sleep(0.2)
        await inp.press("Control+a")
        await asyncio.sleep(0.1)
        await inp.press("Delete")
        await asyncio.sleep(0.1)
        await inp.type(value, delay=30)
        await asyncio.sleep(0.2)
        await inp.press("Enter")
        await asyncio.sleep(0.3)
        await inp.press("Tab")
        await asyncio.sleep(0.3)

    async def _fill_dropdown(self, page: Page, field_id: str, value: str):
        """选择 Element-UI 下拉菜单（精确定位到 form-item 容器内的 el-select）"""
        self.log(f"    下拉 {field_id} = {value}")

        # 精确定位：先找到包含该 label 的 form-item 容器，再在容器内找下拉输入框
        form_item = page.locator(f'.el-form-item:visible:has(label[for="{field_id}"])').first
        dropdown_input = form_item.locator('.el-select .el-input__inner').first
        await dropdown_input.wait_for(state="visible", timeout=10000)
        await dropdown_input.click()
        await asyncio.sleep(0.5)

        # 遍历所有可见的下拉选项
        found = False
        visible_items = page.locator(".el-select-dropdown__item:visible")
        count = await visible_items.count()

        for i in range(count):
            item = visible_items.nth(i)
            item_text = (await item.text_content() or "").strip()

            # 匹配：精确匹配 / 前缀匹配（如 "0" 匹配 "0 -- 全辖"）
            if (item_text == value
                    or item_text.startswith(f"{value} ")
                    or item_text.startswith(f"{value}--")):
                await item.click()
                found = True
                break

        if not found:
            # 兜底：has-text 模糊匹配
            try:
                await page.locator(
                    f".el-select-dropdown__item:visible:has-text('{value}')"
                ).first.click()
                found = True
            except Exception:
                pass

        if not found:
            self.log(f"    下拉选项 '{value}' 未找到，跳过", "WARN")
            await page.keyboard.press("Escape")

        await asyncio.sleep(0.3)

    async def _set_checkbox(self, page: Page, label_text: str, target: int):
        """设置复选框状态（通过 is-checked 类判断）"""
        self.log(f"    复选框 '{label_text}' -> {'勾选' if target == 1 else '取消'}")

        cb_locator = page.locator(f"label.el-checkbox:visible:has-text('{label_text}')").first

        try:
            await cb_locator.wait_for(state="visible", timeout=5000)
        except Exception:
            self.log(f"    复选框 '{label_text}' 未找到（当前数据类型可能无此字段），跳过", "WARN")
            return

        class_attr = await cb_locator.get_attribute("class") or ""
        is_checked = "is-checked" in class_attr

        if (target == 1 and not is_checked) or (target == 0 and is_checked):
            await cb_locator.click()
            await asyncio.sleep(0.3)

    # ========================================================================
    # 查询等待：检测"原样导出"按钮可用状态
    # ========================================================================
    async def _click_query_and_wait(self, page: Page, sz_type: str = "收入"):
        """点击查询按钮，等待"原样导出"按钮 ui-state-enabled。超时6分钟。"""
        self.log("  点击查询按钮...")

        query_btn = page.locator("button.el-button:visible:has-text('查询')").first
        await query_btn.wait_for(state="visible", timeout=5000)
        await query_btn.click()

        self.log("  等待数据查询完成（检测'原样导出'按钮状态，最长6分钟）...")
        await asyncio.sleep(3)

        iframe_name = REPORT_CONFIGS[sz_type]["iframe_name"]
        self.log(f"  使用 iframe: {iframe_name}")
        iframe = page.frame_locator(f'iframe[name="{iframe_name}"]')
        export_indicator = iframe.locator('.fr-btn[widgetname="ExcelO"]')

        max_wait, poll_interval, elapsed = 360, 2, 0
        while elapsed < max_wait:
            try:
                class_attr = await export_indicator.get_attribute("class", timeout=5000)
                if class_attr and "ui-state-enabled" in class_attr:
                    self.log("  数据查询完成，导出按钮已可用", "SUCCESS")
                    await asyncio.sleep(1)
                    return
            except Exception:
                pass
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            if elapsed % 30 == 0:
                self.log(f"  仍在等待数据加载... ({elapsed}秒)", "INFO")

        self.log("  等待导出按钮可用超时（6分钟），继续尝试导出...", "WARN")
        await asyncio.sleep(1)

    # ========================================================================
    # 需求2：强力导出（force=True 穿透透明遮挡层）
    # ========================================================================
    async def _export_and_save(
        self,
        page: Page,
        dl_folder: str,
        dynamic_name: str,
        sz_type: str = "收入",
        keep_original_name: bool = True,
    ) -> str:
        """
        切入 iframe，使用 force=True 点击"原样导出"，保存文件。
        返回保存的绝对路径。
        """
        self.log("  准备导出数据...")

        # 切入帆软报表 iframe（收入6010，支出6020，退库6030，库存6040）
        iframe_name = REPORT_CONFIGS[sz_type]["iframe_name"]
        iframe = page.frame_locator(f'iframe[name="{iframe_name}"]')

        # 使用 get_by_text 精确定位"原样导出"按钮
        export_btn = iframe.get_by_text("原样导出", exact=True)
        await export_btn.wait_for(state="visible", timeout=30000)

        self.log("  已找到导出按钮，force=True 强力点击下载...", "INFO")

        # expect_download 拦截下载 + force=True 穿透透明遮挡层
        async with page.expect_download(timeout=360000) as download_info:
            await export_btn.click(force=True)

        download = await download_info.value
        original_name = download.suggested_filename or "report.xlsx"

        # 默认保持历史行为：{动态名}_{原文件名}；核对专用模板可关闭，精确输出指定文件名。
        ext = os.path.splitext(original_name)[1] or ".xlsx"
        if keep_original_name:
            new_filename = f"{dynamic_name}_{original_name}"
        else:
            new_filename = f"{dynamic_name}{ext}"
        save_path = os.path.join(dl_folder, new_filename)

        await download.save_as(save_path)
        self.log(f"  文件已保存: {new_filename}", "SUCCESS")

        return save_path

    # ========================================================================
    # 数据后处理（openpyxl 保留格式）
    # ========================================================================
    def _process_excel_data(self, file_path: str, row: pd.Series, sz_type: str):
        """使用 openpyxl 后处理，保留原始格式。"""
        self.log("  开始数据后处理（openpyxl 保留格式）...")
        try:
            wb = load_workbook(file_path)
            ws = wb.active
        except Exception as e:
            self.log(f"  读取导出文件失败: {e}", "ERROR")
            return

        if ws.max_row is None or ws.max_row < 2:
            self.log("  导出文件为空，跳过后处理", "WARN")
            wb.close()
            return

        if sz_type == "库存":
            stock_city_col = None
            for c in range(1, ws.max_column + 1):
                header = str(ws.cell(row=1, column=c).value or "").strip()
                if header == "所属市国库代码":
                    stock_city_col = c
                    break

            if stock_city_col is None:
                self.log("    未找到'所属市国库代码'列，库存专用清理跳过", "INFO")
            else:
                ws.delete_cols(stock_city_col, 1)
                self.log(f"    已删除库存报表'所属市国库代码'列（原第{stock_city_col}列）", "SUCCESS")

            try:
                wb.save(file_path)
                self.log("  库存数据后处理完成，已覆盖保存（格式已保留）", "SUCCESS")
            except Exception as e:
                self.log(f"  保存库存后处理文件失败: {e}", "ERROR")
            finally:
                wb.close()
            return

        bdg_level = str(row.get("pBdgLevel", "")).strip()
        bdg_code = bdg_level.split("--")[0].strip() if "--" in bdg_level else bdg_level
        # 全口径(代码为0或空) → "0"；地方级(省2,3,4,5 / 市3,4,5 / 县4,5等) → "6"
        if bdg_code == "0" or bdg_code == "":
            budget_level = "0"
        else:
            budget_level = "6"

        new_col = ws.max_column + 1
        ws.cell(row=1, column=new_col, value="预算级次")
        for r in range(2, ws.max_row + 1):
            ws.cell(row=r, column=new_col, value=budget_level)
        self.log(f"    已在末尾新增'预算级次'列（第{new_col}列），值={budget_level}")

        by_tre = str(row.get("pByTre", "")).strip()
        tre_code = by_tre.split("--")[0].strip() if "--" in by_tre else by_tre.strip()

        if tre_code == "3":
            # 市级报表，需要剔除计划单列市
            plan_cities = {"深圳市", "厦门市", "青岛市", "宁波市", "大连市"}

            # 动态查找"国库简称"列
            city_col = None
            for c in range(1, ws.max_column + 1):
                header = str(ws.cell(row=1, column=c).value or "").strip()
                if header == "国库简称":
                    city_col = c
                    break

            if city_col is None:
                self.log("    未找到'国库简称'列，跳过计划单列市剔除", "WARN")
            else:
                self.log(f"    '国库简称'列位于第{city_col}列，开始检查计划单列市...")
                rows_to_delete = []
                for r in range(2, ws.max_row + 1):
                    if str(ws.cell(row=r, column=city_col).value or "").strip() in plan_cities:
                        rows_to_delete.append(r)
                removed = 0
                for r in reversed(rows_to_delete):
                    ws.delete_rows(r, 1)
                    removed += 1
                if removed > 0:
                    self.log(f"    已剔除 {removed} 行计划单列市数据", "SUCCESS")
                else:
                    self.log("    未发现计划单列市数据需要剔除")

        # 新增：将特定列的格式设置为“常规” (General)
        target_formats = {"本期执行数", "同期执行数", "同比增速(%)", "年累计", "同期年累计", "年累计同比增速(%)"}
        format_cols = []
        for c in range(1, ws.max_column + 1):
            header = str(ws.cell(row=1, column=c).value or "").strip()
            if header in target_formats:
                format_cols.append(c)
        
        if format_cols:
            self.log(f"    开始将 {len(format_cols)} 列的单元格格式设置为'常规'...")
            for c in format_cols:
                for r in range(2, ws.max_row + 1):
                    ws.cell(row=r, column=c).number_format = 'General'
            self.log("    单元格格式设置完成", "SUCCESS")

        try:
            wb.save(file_path)
            self.log("  数据后处理完成，已覆盖保存（格式已保留）", "SUCCESS")
        except Exception as e:
            self.log(f"  保存后处理文件失败: {e}", "ERROR")
        finally:
            wb.close()


# ============================================================================
# Excel参数模板生成
# ============================================================================
def generate_template(output_path: str = "TMIS数据自由查询参数模板_v5.1.xlsx"):
    """生成数据自由查询参数模板（分收入/支出/退库/库存 Sheet，全中文表头）"""
    with pd.ExcelWriter(output_path) as writer:
        for report_type, config in REPORT_CONFIGS.items():
            columns = config["template_columns"]
            sample = config["sample"]
            df = pd.DataFrame([sample], columns=columns)
            df.to_excel(writer, sheet_name=f"{report_type}参数", index=False)

    print(f"参数模板已生成: {output_path}")


# ============================================================================
# 入口
# ============================================================================
def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--template":
        out = sys.argv[2] if len(sys.argv) > 2 else "TMIS数据自由查询参数模板_v5.1.xlsx"
        generate_template(out)
        return

    app = TMISAutoApp()

    app.log("TMIS 数据自由查询批量抓取工具 v5.1 就绪", "SUCCESS")
    app.log("")
    app.log("全自动使用步骤")
    app.log("  1  选择 Excel 参数模板")
    app.log("  2  选择下载保存目录")
    app.log("  3  设置日期、命名模式、是否启用后处理")
    app.log("  4  点击「捕获登录链接」")
    app.log("  5  在客户端触发系统登录")
    app.log("  6  程序自动拦截 Token -> 启动 Chrome -> 执行全部任务")
    app.log("")
    app.log("无需手动打开浏览器，自动检测系统 Chrome，全程零操作", "SUCCESS")

    try:
        app.mainloop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
