"""TMIS 参数校验和跨平台运行辅助，不依赖 GUI。"""

import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


def option_matches(value, label):
    """接受完整选项文本或独立代码，避免 0 匹配到 10。"""
    value, label = str(value).strip(), str(label).strip()
    if value == label:
        return True
    return "--" not in value and re.split(r"\s*--\s*", label, maxsplit=1)[0] == value


def normalize_query_date(value, report_type=""):
    """兼容 Excel 日期单元格，以及旧模板 YYYY/MM/DD 紧凑字符串。"""
    text = str(value).strip()
    if not text:
        return ""
    code = str(report_type).split("--", 1)[0].strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T]00:00:00)?", text):
        date = datetime.strptime(text[:10], "%Y-%m-%d")
        return date.strftime({"3": "%Y%m", "5": "%Y"}.get(code, "%Y%m%d"))
    formats = {4: "%Y", 6: "%Y%m", 8: "%Y%m%d"}
    if not text.isdigit() or len(text) not in formats:
        raise ValueError("日期应为 YYYY、YYYYMM 或 YYYYMMDD：" + text)
    datetime.strptime(text, formats[len(text)])
    expected = {"1": 8, "3": 6, "5": 4}.get(code)
    if expected and len(text) != expected:
        raise ValueError("报表类型 %s 的日期应为 %s 位，收到 %s" % (code, expected, text))
    return text


def normalize_task_row(row):
    """每条任务开始前校验日期与复选框，不把空单元格解释成 0。"""
    result = row.copy()
    for key in ("pStartDate", "pEndDate"):
        result[key] = normalize_query_date(result.get(key, ""), result.get("pRptType", ""))
    start, end = result.get("pStartDate", ""), result.get("pEndDate", "")
    if start and end and (len(start) != len(end) or start > end):
        raise ValueError("起止日期的格式不一致，或起始日期晚于终止日期")
    return result


def validate_login_url(value):
    url = str(value).strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or any(c.isspace() for c in url):
        raise ValueError("请粘贴完整的 http:// 或 https:// 登录链接")
    return url


def redact_urls(message):
    # Playwright 异常会附带 goto 的完整地址，因此不仅处理主动日志。
    return re.sub(r"https?://[^\s<>\"']+", "[链接已隐藏]", str(message))


def browser_launch_options(manual_path="", bundled_path=""):
    """手动路径优先；Linux 默认使用匹配版本的随包浏览器。"""
    manual_path = str(manual_path).strip()
    if manual_path:
        path = os.path.expanduser(manual_path)
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise ValueError("浏览器路径不存在或不可执行：" + path)
        return {"executable_path": path}
    if sys.platform.startswith("linux"):
        if bundled_path and Path(bundled_path).is_file():
            return {"executable_path": str(bundled_path)}
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
            path = shutil.which(name)
            if path:
                return {"executable_path": path}
        raise ValueError("未找到 Chrome/Chromium。请完整解压 Linux 包，或填写系统浏览器的可执行路径。")
    return {"channel": "chrome"}


def browser_environment():
    """外部浏览器不应加载 PyInstaller 为 Python 设置的动态库搜索路径。"""
    env = os.environ.copy()
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
    return env


def bundled_browser_path():
    root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return root / "browser" / "chrome-linux" / "chrome"
