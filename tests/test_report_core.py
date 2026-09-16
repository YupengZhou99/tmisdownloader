"""验证本次江苏逐月逐库任务和 Linux 启动参数，均不访问内网。"""

import calendar
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tmis_runtime import (
    browser_environment, browser_launch_options, normalize_query_date,
    normalize_task_row, option_matches, redact_urls, validate_login_url,
)

SPEC = importlib.util.spec_from_file_location("report_app", ROOT / "TMIS数据批量抓取工具V5.0_副本.py")
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)
PLAN = json.loads((ROOT / "scripts/jiangsu_stock_plan.json").read_text())


class ParameterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sheets = pd.read_excel(ROOT / PLAN["output_file"], sheet_name=None, dtype=str)
        cls.data = cls.sheets["库存参数"].fillna("")

    def test_280_unique_month_treasury_tasks_and_exact_codes(self):
        self.assertEqual(len(self.data), 280)
        codes = {str(1000000000 + i * 1000000) for i in range(2, 14)} | {"1015000000", "1000000000"}
        self.assertEqual(set(self.data["国库选择"]), codes)
        self.assertEqual(self.data["文件名称"].nunique(), 280)
        for code in codes:
            self.assertEqual(len(self.data[self.data["国库选择"] == code]), 20)
        self.assertEqual([name for name in self.sheets if any(t in name for t in APP.REPORT_CONFIGS)], ["库存参数"])

    def test_dates_cover_every_day_of_each_requested_month(self):
        pairs = set()
        for _, raw in self.data.iterrows():
            row = normalize_task_row(raw.rename(index=APP.COLUMN_MAPPING))
            start, end = row["pStartDate"], row["pEndDate"]
            year, month = int(start[:4]), int(start[4:6])
            self.assertEqual(start[6:], "01")
            self.assertEqual(end, start[:6] + str(calendar.monthrange(year, month)[1]))
            pairs.add((start[:6], row["pTreCode"]))
        expected_months = {"2025%02d" % m for m in range(1, 13)} | {"2026%02d" % m for m in range(1, 9)}
        self.assertEqual({month for month, _ in pairs}, expected_months)
        self.assertEqual(len(pairs), 280)

    def test_scope_subjects_units_and_checkboxes_match_request(self):
        for _, row in self.data.iterrows():
            expected_scope = "1 -- 下级" if row["国库选择"] == "1000000000" else "0 -- 全部"
            self.assertEqual(row["展示范围"], expected_scope)
            self.assertEqual(row["会计科目"], "17202,271,27201,27202,27203,273")
            self.assertEqual(row["报表类型"], "1 -- 日")
            self.assertEqual(row["金额单位"], "0 -- 元")
            self.assertEqual(row["会计账户名称"], "")
            for name in ("分序时", "分地区", "分预算级次", "分会计科目", "分会计账户"):
                self.assertEqual(row[name], "1")
            for name in ("分国库属性", "是否展示同比", "是否追加日期", "保留原文件名"):
                self.assertEqual(row[name], "0")

    def test_exported_xlsx_has_real_dates_and_software_headers(self):
        with open(ROOT / PLAN["output_file"], "rb") as stream:
            book = load_workbook(stream, read_only=True, data_only=True)
            sheet = book["库存参数"]
            self.assertEqual([cell.value for cell in sheet[1]], APP.REPORT_CONFIGS["库存"]["template_columns"])
            self.assertEqual(sheet["F2"].value.strftime("%Y%m%d"), "20250101")
            self.assertEqual(sheet["G281"].value.strftime("%Y%m%d"), "20260831")
            self.assertEqual(book["使用说明"]["B7"].value, 280)
            book.close()

    def test_task_filename_uses_exact_parameter_name(self):
        app = type("Stub", (), {"run_options": {"naming_mode": "param", "start_date": "", "end_date": ""}})()
        for _, raw in self.data.iloc[[0, 13, 279]].iterrows():
            row = normalize_task_row(raw.rename(index=APP.COLUMN_MAPPING))
            self.assertEqual(APP.TMISAutoApp._build_filename(app, row, "库存"), raw["文件名称"])
            self.assertFalse(APP.TMISAutoApp._should_keep_original_name(app, row))


class RuntimeTests(unittest.TestCase):
    def test_date_validation_and_old_template_compatibility(self):
        self.assertEqual(normalize_query_date("2025-02-28 00:00:00", "1 -- 日"), "20250228")
        self.assertEqual(normalize_query_date("202601", "3 -- 月"), "202601")
        self.assertEqual(normalize_query_date("2024", "5 -- 年"), "2024")
        self.assertEqual(normalize_query_date("20240229", "1"), "20240229")
        for date, kind in (("20250229", "1"), ("202501", "1"), ("20251301", "1")):
            with self.assertRaises(ValueError):
                normalize_query_date(date, kind)
        with self.assertRaises(ValueError):
            normalize_task_row({"pRptType": "1", "pStartDate": "20260131", "pEndDate": "20260101"})

    def test_dropdown_matching_does_not_use_substring(self):
        self.assertTrue(option_matches("0", "0 -- 全部"))
        self.assertTrue(option_matches("1 -- 下级", "1 -- 下级"))
        self.assertFalse(option_matches("0", "10 -- 其他"))
        self.assertFalse(option_matches("1 -- 下级", "1 -- 本级"))

    def test_url_is_validated_and_exception_logs_are_redacted(self):
        url = "https://example.invalid/login?token=do-not-log#secret"
        self.assertEqual(validate_login_url(" " + url + " "), url)
        for bad in ("", "file:///tmp/token", "http://", "https://example.invalid/a b"):
            with self.assertRaises(ValueError):
                validate_login_url(bad)
        self.assertNotIn("do-not-log", redact_urls("goto failed: " + url))

    def test_manual_executable_is_honored_and_invalid_path_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "chromium"
            executable.touch()
            executable.chmod(0o755)
            self.assertEqual(browser_launch_options(str(executable)), {"executable_path": str(executable)})
            with self.assertRaises(ValueError):
                browser_launch_options(str(executable) + ".missing")

    def test_linux_system_and_packaged_browser_fallback(self):
        with tempfile.TemporaryDirectory() as directory, patch("sys.platform", "linux"):
            executable = Path(directory) / "chrome"
            executable.touch()
            with patch("shutil.which", return_value="/usr/bin/chromium"):
                self.assertEqual(browser_launch_options("", executable)["executable_path"], str(executable))
                self.assertEqual(browser_launch_options("", "")["executable_path"], "/usr/bin/chromium")
            with patch("shutil.which", return_value=None):
                self.assertEqual(browser_launch_options("", executable)["executable_path"], str(executable))

    def test_frozen_external_browser_does_not_inherit_python_libraries(self):
        with patch("sys.platform", "linux"), patch.object(sys, "frozen", True, create=True):
            with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/frozen/libs"}, clear=True):
                self.assertNotIn("LD_LIBRARY_PATH", browser_environment())
                self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/frozen/libs")
            with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/frozen/libs", "LD_LIBRARY_PATH_ORIG": "/user/libs"}, clear=True):
                self.assertEqual(browser_environment()["LD_LIBRARY_PATH"], "/user/libs")


if __name__ == "__main__":
    unittest.main()
