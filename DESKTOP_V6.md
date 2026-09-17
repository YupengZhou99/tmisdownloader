# TMIS Workbench V6 · 麒麟 x64 桌面版

浅色 Electron + React + TypeScript 界面，复用原 Python 报表引擎与 SQLite 队列。收入、支出、库存的数据自由查询字段和导出规则沿用既有程序；不是“总库数据自由查询”。

## 下载与启动

在 GitHub Actions 的 **Build TMIS Workbench V6 Kylin x64** 成功运行中下载 `TMIS-Workbench-V6-Kylin-x64` artifact。解开 artifact ZIP，再解开其中的 `.tar.gz`，完整保留目录结构。在图形桌面的终端运行：

```bash
cd TMIS-Workbench-Kylin-x64
./启动.sh
```

目标：银河麒麟 V10 SP1、x86_64、glibc ≥ 2.31。内含 Python 后台、Electron 和抓取用 Chromium，不需要联网安装 Python / Node / 浏览器。不要将压缩包内某一个文件单独复制出来。

使用普通桌面用户，不使用 sudo。为悬浮窗的位置、置顶和吸附功能，应用使用 X11 / XWayland；纯 Wayland 且没有 XWayland 的桌面不适用此启动方式。OS 窗口管理器仍可能限制置顶行为。

若报 Chromium sandbox / user namespace 错误，请系统管理员核对麒麟安全策略，不要直接添加 `--no-sandbox`。CI 的 root 容器验收例外不会写进发布启动器。

## 推荐使用顺序

1. 点击“连接 TMIS”，粘贴完整登录 URL。无需先选参数、输出目录。Linux 不捕获登录 URL。
2. 点击“添加参数表”，多选或拖入 Excel；选择下载目录，点击“检查参数”。
3. 确认识别的报表类型、条数、时间范围和样例字段，再点击“加入队列”。
4. 点击“开始 / 继续”。运行中可以继续导入，新任务排在队尾；空闲时导入不会自动开始。
5. 结束后浏览器与服务继续开启。“待处理任务”集中展示失败、超时和中断，可逐条、选中或全部重试。成功任务不会因重试按钮重复执行。

每份参数表生成独立输出子目录，保留 `下载结果.csv` 和 `重试记录.csv`。相同内容再次导入需要明确确认，并使用新的目录。队列存储的是导入时的参数、命名方式、后处理选项和输出目录快照；之后修改 Excel 不会改动队列。检查预览有效 15 分钟。

默认不开启 Excel 后处理，保留原始导出；开启后沿用旧程序的清洗 / 汇总规则，可能改变数据内容。

## 窗口与浏览器的区别

| 操作 | 运行中行为 | 是否重启抓取浏览器 |
| --- | --- | --- |
| 主界面 ↔ 悬浮窗 | 立即切换，共享同一队列与进度 | 否 |
| 浏览器最小化 / 还原 | 保留当前页面与登录，只改变窗口状态 | 否 |
| 有头 ↔ 无头 | 等当前条完成或失败，再切换；执行前可取消预约 | 是 |
| 暂停队列 | 当前条结束后暂停，后续任务不领取 | 否 |
| 队列结束 | 服务待命，允许继续追加和重试 | 否 |
| 明确退出应用 | 关闭浏览器、保存中断任务、停止服务 | 是 |

快捷键 `Ctrl + Shift + M`（macOS 为 `⌘ + Shift + M`）在工作台与悬浮窗间切换。悬浮窗可拖动、置顶开关、靠近屏幕边缘吸附；位置会记住，并在屏幕变化时约束到可见范围。

真正的有头 / 无头不是窗口隐藏。切换时仅在内存中迁移 Cookie、localStorage、sessionStorage，然后重建浏览器、检查工作界面和登录状态。不会重放登录 URL 中的 token / ticket。当前固定的 Playwright 版本不迁移 IndexedDB、内存中的页面对象或服务器绑定浏览器的状态，因此 **不能保证所有 TMIS 登录会话都可迁移**。迁移失败会保留成功记录及剩余任务，等待新的登录链接，不连续消耗后续任务。

## 本地保存与安全边界

- 抓取后台通过父子进程私有标准输入输出通信，不监听 HTTP 端口。
- UI 只加载包内资源，禁用 Node integration，启用 renderer sandbox、context isolation、CSP 和 IPC 来源检查。无 CDN 字体、遥测、云端上传或自动更新。
- 应用不主动把登录 URL、token、Cookie 或浏览器登录状态写入配置、任务库和日志；错误日志脱敏。运行时浏览器/操作系统仍可能使用受自身策略管理的临时文件和内存交换，不能将其宣称为取证意义上的“零落盘”。
- Linux 任务库默认在 `~/.local/state/tmis-dler`（遵从 XDG_STATE_HOME），与旧队列格式兼容。macOS 在 `~/Library/Application Support/TMISDLer`。任务参数、下载路径及报表本身按本机敏感数据管理。
- 主窗口关闭会询问继续运行、悬浮窗或退出。没有静默托盘常驻；悬浮窗关闭回到工作台。
- 突然断电 / 强杀进程后，下次启动显示中断任务；需重新登录和继续，不自动复用过期链接。若退出恰逢文件已经保存但尚未记成功，重试可能提示文件已存在，请核对文件；不会自动覆盖。

## 参数表口径提醒

本次只打包程序，不内置旧业务参数表。请使用你已确认的最新版收入、支出和库存参数表；旧库存 280 条表包含原“全辖”省级口径，不能代替后续确认的“本级”修订参数。不要重复导入旧表的省级行与修订省级表。程序不会擅自修改已有 Excel。

## 本机开发与验证

```bash
cd desktop
npm ci
npm run build
npm start
```

开发入口默认使用上级目录 `.venv/bin/python`；可用 `TMIS_PYTHON` 指定具备 requirements-linux-x64.txt 对应依赖的解释器。旧 Tk 界面仍可通过原 Python 主文件启动，但不能与 V6 同时使用同一任务库。

`npm run dev` 提供只读外观预览（127.0.0.1:5173）；浏览器预览没有本机文件和任务服务。`TMIS_DEV_URL=http://127.0.0.1:5173 npm start` 可用于桌面开发。生产包不读取该 URL。

验证入口：

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
TMIS_BROWSER_TESTS=1 python -m unittest discover -s tests -p 'test_report_browser.py' -v
npm --prefix desktop test
node desktop/scripts/smoke.cjs
```

桌面验收脚本使用独立临时任务库、合成 localhost TMIS 页面和参数表，验证真实 Electron → IPC → Python → Chromium 下载、悬浮窗切换、会话迁移、保留浏览器以及退出。不接触真实 TMIS、用户任务库或业务数据。CI 同时在 glibc 2.31 环境执行打包后的应用；截图和验收 JSON 是独立 artifact。

**验收边界：** 构建成功、Mac 本机交互或 Debian 兼容环境测试，均不能替代实际麒麟桌面窗口管理器、系统安全策略、TMIS 内网权限和真实报表内容验收。首次使用请先选一个市级和一个省级任务，人工核对导出后再运行完整队列。
