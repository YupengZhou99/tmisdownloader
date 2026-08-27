# TMIS 登录 Token 捕获工具（Windows 7 32 位）

## 用途

这是从原 TMIS 批量下载程序中拆出的独立工具，只负责捕获 TMIS 客户端启动浏览器时携带的登录 URL。它不会启动 Playwright、不会访问 TMIS 页面、不会读取 Excel，也不会自动执行后续下载。

## 推荐运行环境

- Windows 7 SP1 32 位；
- 系统已安装 Windows 7 的最新可用补丁；
- 如 EXE 提示缺少 `api-ms-win-crt-*.dll` 或 `VCRUNTIME140.dll`，需安装 Microsoft Visual C++ 2015-2022 Redistributable x86，并确认 Universal C Runtime 更新已安装；
- 发布 EXE 为 32 位，可同时运行在 32 位和 64 位 Windows 上，但主要兼容目标是 Win7 x86。

## 使用步骤

1. 双击 `TMIS-Token-Sniper-Win7-x86.exe`。
2. 建议先关闭无关浏览器窗口，避免默认浏览器复用旧进程。
3. 保持 URL 关键字为 `tsas`；如果实际登录 URL 不含该字符串，再清空关键字重试。
4. 保持“捕获后终止新浏览器进程”勾选，以免 Token 被原浏览器消费。
5. 点击“开始监控”。
6. 回到 TMIS 客户端触发登录。
7. 捕获成功后，完整 URL 会显示在结果框，并默认自动复制到剪贴板。
8. 将 URL 粘贴到后续需要使用 Token 的程序中。

完整 URL 属于敏感登录凭证。不要发到聊天群、邮件或日志中；使用完成后建议复制其他普通文字覆盖剪贴板。

## 监控范围

程序识别以下新启动的浏览器进程：

- Chrome；
- Edge；
- Firefox；
- Internet Explorer；
- 360 安全浏览器/极速浏览器；
- QQ 浏览器；
- 搜狗浏览器。

点击“开始监控”之前已经运行的浏览器进程会被忽略。关键字为空时，程序会捕获第一个新浏览器命令行中的 HTTP(S) URL，因此监控期间不要打开无关网页。

## 常见问题

- 一直超时：先清空 `tsas` 关键字重试，并确认 TMIS 客户端确实启动了新的浏览器进程。
- 已有浏览器打开时抓不到：关闭所有浏览器后重新开始监控，再触发一次 TMIS 登录。
- 能看到浏览器但抓不到或无法终止：TMIS 客户端/浏览器可能以管理员权限运行，请以相同权限运行本工具。
- 成功捕获但未自动复制：点击“复制完整 URL”，或直接在结果框中全选复制。
- EXE 无法启动并提示 CRT/DLL 缺失：按“推荐运行环境”安装 Win7 补丁与 Visual C++ x86 运行库。

## 与原版 URL 狙击手相比

- 可以主动停止监控，不必等待 180 秒；
- 可以用 URL 关键字降低误捕获概率；
- 捕获结果与运行日志分离，日志不会写入完整 Token；
- 使用 Tkinter 自带剪贴板，不再依赖 pyperclip；
- 不包含 Playwright、pandas、openpyxl 等大依赖；
- 仍保留可选的“捕获后终止新浏览器”行为。

## 从源码运行

Win7 目标环境请使用 Python 3.8.10 32 位：

```bat
py -3.8-32 -m pip install psutil==5.9.8
py -3.8-32 "TMIS登录Token捕获工具_Win7_32位.py"
```

## 构建 EXE

仓库提供 `.github/workflows/build-token-sniper-win7-x86.yml`。手动运行该 GitHub Actions 工作流后，下载名为 `TMIS-Token-Sniper-Win7-x86` 的 artifact。

构建链固定为：

- Python 3.8.10 x86；
- psutil 5.9.8；
- PyInstaller 4.10；
- PE 架构校验必须为 `0x014C`（Intel 386 / 32 位 Windows）。

由于当前开发机不是 Windows，本地无法替代真实 Win7 32 位机器做最终启动验证。发布前至少应在一台 Win7 SP1 x86 机器上完成一次启动、捕获、终止浏览器和复制剪贴板的冒烟测试。

## 兼容性依据

- [Python 官方 Windows 文档](https://docs.python.org/3/using/windows.html)明确建议需要 Windows 7 支持时使用 Python 3.8。
- [Python 3.8.10 发布页](https://www.python.org/downloads/release/python-3810/)提供 Windows 32 位安装包。
- [psutil 5.9.8 发布页](https://pypi.org/project/psutil/5.9.8/)提供 Windows x86 wheel，并将 Windows 7 列为支持平台。
- [PyInstaller 4.10 发布页](https://pypi.org/project/pyinstaller/4.10/)提供 Windows x86 wheel，并说明 Windows 7 应可运行。
- [actions/setup-python](https://github.com/actions/setup-python)支持显式选择 `x86` Python，用于生成真正的 32 位 EXE。
