# -*- coding: utf-8 -*-
"""不启动 GUI 的 Token 捕获核心逻辑测试。"""

import importlib.util
import pathlib
import unittest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "TMIS登录Token捕获工具_Win7_32位.py"
SPEC = importlib.util.spec_from_file_location("token_sniper_win7_x86", str(MODULE_PATH))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TokenSniperCoreTests(unittest.TestCase):
    def test_supported_browser_name_accepts_path_and_case(self):
        self.assertTrue(MODULE.is_supported_browser(r"C:\Program Files\Google\Chrome\chrome.exe"))
        self.assertTrue(MODULE.is_supported_browser("IEXPLORE.EXE"))
        self.assertFalse(MODULE.is_supported_browser("notepad.exe"))

    def test_extracts_url_from_plain_and_prefixed_arguments(self):
        cmdline = [
            "chrome.exe",
            "--new-window",
            'launcher=https://tsas.example/login?token=abc123',
            "http://fallback.example/path",
        ]
        self.assertEqual(
            MODULE.extract_http_urls(cmdline),
            [
                "https://tsas.example/login?token=abc123",
                "http://fallback.example/path",
            ],
        )

    def test_keyword_selects_expected_url(self):
        cmdline = [
            "chrome.exe",
            "https://unrelated.example/",
            "https://tsas.example/login?token=secret",
        ]
        self.assertEqual(
            MODULE.select_matching_url(cmdline, "TSAS"),
            "https://tsas.example/login?token=secret",
        )
        self.assertIsNone(MODULE.select_matching_url(cmdline, "missing"))

    def test_existing_or_non_browser_process_is_ignored(self):
        cmdline = ["chrome.exe", "https://tsas.example/login?token=secret"]
        self.assertIsNone(
            MODULE.match_new_browser_process(100, "chrome.exe", cmdline, {100}, "tsas")
        )
        self.assertIsNone(
            MODULE.match_new_browser_process(101, "notepad.exe", cmdline, set(), "tsas")
        )

    def test_new_browser_returns_matching_url(self):
        url = "https://tsas.example/login?token=secret"
        self.assertEqual(
            MODULE.match_new_browser_process(101, "chrome.exe", ["chrome.exe", url], {100}, "tsas"),
            url,
        )

    def test_log_summary_does_not_include_token_value(self):
        summary = MODULE.summarize_url("https://tsas.example/login?token=secret&user=abc")
        self.assertEqual(summary, "https://tsas.example/login?...")
        self.assertNotIn("secret", summary)
        self.assertNotIn("user=abc", summary)


if __name__ == "__main__":
    unittest.main()
