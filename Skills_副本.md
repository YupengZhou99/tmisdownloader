# TMIS 抓取工具 — 技术经验沉淀 (Skills)

> 本项目在开发过程中积累的**可复用技巧与最佳实践**，以供后续类似 RPA / Web 自动化项目参考。

---

## 1. Element-UI 表单精确定位策略

### 问题
Element-UI 组件的 `input` 元素通常没有唯一 ID，多个下拉框共享相同的 `.el-input__inner` 类名。直接使用 `label[for="xxx"] ~ div` 的兄弟选择器在复杂布局中不稳定。

### 解决方案：容器定位法（两步法）

```python
# 第一步：通过 label[for] 锚定当前可见 form-item 容器
form_item = page.locator(f'.el-form-item:visible:has(label[for="{field_id}"])').first

# 第二步：在容器内精确查找目标控件
dropdown = form_item.locator('.el-select .el-input__inner').first
date_inp = form_item.locator('.el-date-editor .el-input__inner').first
text_inp = form_item.locator('.el-input .el-input__inner').first
```

### 核心原理
- `.el-form-item:visible:has(label[for="xxx"])` 利用 CSS `:has()` 伪类，选中**包含**指定 `label` 的当前可见表单项容器
- 然后在容器内部用 `.el-select` / `.el-date-editor` 等二级选择器精确锁定目标
- 这种方式比 `~`（兄弟选择器）更稳定，不受 DOM 层级变化影响
- 在多标签页 SPA 中加 `:visible` 很关键，可避免隐藏旧标签页里的同名字段被 `.first` 命中

---

## 2. 多标签页 DOM 冲突解决方案

### 问题
类似 TMIS 的单页应用（SPA）使用 Element-UI Tabs，当同时打开"收入查询"和"支出查询"标签页时，两套表单的 DOM 同时存在。使用 `.first` 全局查找定位器会匹配到隐藏的旧标签页中的元素，导致 Timeout。

### 解决方案：按需关闭（Smart Tab Close）

```python
async def _close_current_tab(self, page, sz_type):
    tab_name = REPORT_CONFIGS[sz_type]["menu_title"]
    close_btn = page.locator(
        f"span.tags-view-item:has-text('{tab_name}')"
    ).locator(".el-icon-close").first
    if await close_btn.is_visible(timeout=3000):
        await close_btn.click()
        self.current_nav_type = None  # 重置状态
```

### 策略对比

| 策略 | 优点 | 缺点 |
|---|---|---|
| 每次任务后关闭 | 绝对干净 | 浪费时间重复加载 |
| 限定在活跃 Tab Pane 中查找 | 无需关闭 | 需要知道活跃容器的精确选择器 |
| **仅在类型切换时关闭（推荐）** | 同类任务复用标签页（最快），异类任务自动清理 | 需维护导航状态标记 |

---

## 3. Token URL 狙击手（进程级拦截）

### 场景
内网系统通过客户端弹出浏览器并在 URL 中携带 Token 参数进行认证。传统的"手动复制 URL"效率低下。

### 实现原理

```python
# 10ms 极限轮询间隔
while not captured:
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if proc.info["name"].lower() in browser_names:
            if proc.info["pid"] not in known_pids:
                for arg in proc.info["cmdline"]:
                    if arg.startswith("http"):
                        captured_url = arg
                        proc.kill()  # 立即击杀防止请求发出
                        break
    time.sleep(0.01)  # 10ms
```

### 关键设计
1. **启动前快照** `known_pids`：避免误杀已存在的浏览器进程
2. **10ms 极限轮询**：在浏览器完成 HTTP 请求之前截断进程
3. **捕获后自动触发**：`self.after(500, self._start_task)` 无缝衔接

---

## 4. 帆软报表 iframe 交互技巧

### 挑战
帆软报表渲染在 iframe 中，且导出按钮可能被透明遮罩层覆盖。

### 解决方案

```python
# 1. 通过 iframe name 切入（收入6010，支出6020，退库6030）
iframe = page.frame_locator(f'iframe[name="{REPORT_CONFIGS[sz_type]["iframe_name"]}"]')

# 2. 通过按钮 widgetname 属性检测查询完成状态
export_btn = iframe.locator('.fr-btn[widgetname="ExcelO"]')
class_attr = await export_btn.get_attribute("class")
is_ready = "ui-state-enabled" in class_attr

# 3. force=True 穿透透明遮罩层进行点击
await export_btn.click(force=True)

# 4. expect_download 拦截真实文件下载
async with page.expect_download(timeout=360000) as download_info:
    await export_btn.click(force=True)
download = await download_info.value
await download.save_as(save_path)
```

---

## 5. openpyxl 保留格式的后处理

### 为什么不用 pandas 处理导出文件？
帆软导出的 Excel 含有丰富的合并单元格、边框、颜色等格式。使用 `pandas.read_excel` → `to_excel` 会丢失全部格式。`openpyxl` 的 `load_workbook` 可以在保留原有格式的前提下做精准修改。

### 常用操作

```python
wb = load_workbook(file_path)
ws = wb.active

# 1. 在末尾新增一列
new_col = ws.max_column + 1
ws.cell(row=1, column=new_col, value="预算级次")
for r in range(2, ws.max_row + 1):
    ws.cell(row=r, column=new_col, value="6")

# 2. 按条件删除行（从后往前，避免索引错乱）
rows_to_delete = [r for r in range(2, ws.max_row + 1) 
                  if ws.cell(row=r, column=city_col).value in plan_cities]
for r in reversed(rows_to_delete):
    ws.delete_rows(r, 1)

# 3. 设置单元格数字格式为"常规"
ws.cell(row=r, column=c).number_format = 'General'

wb.save(file_path)
wb.close()
```

> **注意**：删除行时必须**从后往前** (`reversed`)，否则后续行号会发生偏移导致错删。

---

## 6. Excel 驱动的参数化设计模式

### 核心思想
将系统 HTML 表单的字段 ID（如 `pRptType`）与 Excel 中文表头（如 `报表类型`）通过字典进行双向映射：

```python
COLUMN_MAPPING = {
    "报表库选择": "pRptDbType",
    "调整期标志": "pTrimFlag",
    "报表类型": "pRptType",
    # ... 
}
```

### 好处
- **用户友好**：维护 Excel 模板时只需看到中文表头
- **代码解耦**：内部逻辑始终使用 `pXXX` 变量名，与页面元素 `label[for]` 直接对应
- **向后兼容**：老模板如果已有 `pXXX` 列名，代码会直接识别无需映射

---

## 7. Tkinter 深色主题最佳实践

### 颜色系统设计

```python
class TMISAutoApp(tk.Tk):
    BG_PRIMARY   = "#1a1a2e"    # 主背景（深靛蓝）
    BG_CARD      = "#16213e"    # 卡片背景
    BG_INPUT     = "#0f3460"    # 输入框背景
    BG_LOG       = "#0d1117"    # 日志区背景（近纯黑）
    FG_PRIMARY   = "#e8e8e8"    # 主文字
    FG_ACCENT    = "#00d2ff"    # 强调色（青蓝）
    CLR_GREEN    = "#00e676"    # 成功绿
    CLR_RED      = "#ff5252"    # 错误红
```

### 关键技巧
1. **类常量颜色系统**：所有颜色统一定义，随时全局换肤
2. **ttk.Style + tk.Button 混用**：ttk 负责标准控件风格，tk.Button 实现悬浮变色
3. **线程安全日志**：通过 `self.after(0, callback)` 将日志写入放到主线程队列

---

## 8. 异步 Playwright + Tkinter 线程模型

### 架构

```
┌─────────────┐      ┌──────────────────┐
│  Main Thread │      │  Worker Thread   │
│  (Tkinter)   │─────>│  asyncio.run()   │
│  UI 渲染     │      │  Playwright 操作  │
│  self.after() │<─────│  self.log() 回调  │
└─────────────┘      └──────────────────┘
```

### 关键实现
- `threading.Thread(target=_worker, daemon=True)` 启动后台线程
- 后台线程中 `asyncio.run(_run_automation())` 运行异步代码
- 所有 UI 更新通过 `self.after(0, callback)` 调度回主线程
- `stop_flag` 全局标志实现跨线程安全停止

---

## 9. 多页面自由查询的配置驱动模式

### 问题
收入、支出、退库、库存等自由查询页面技术路径相似，但菜单名称、iframe、字段集合、复选框集合并不完全一致。如果在导航、填表、导出等函数中硬编码分支，后续扩展库存会牵一发动全身。

### 解决方案：集中页面配置

```python
REPORT_CONFIGS = {
    "收入": {
        "menu_title": "收入数据自由查询",
        "iframe_name": "fineReportTsasRpt6010",
        "dropdown_fields": [...],
        "date_fields": ["pStartDate", "pEndDate"],
        "text_fields": [...],
        "checkbox_fields": [...],
        "template_columns": [...],
        "sample": {...},
    },
}
```

### 好处
- 新增页面时优先新增配置，减少修改执行链路。
- `generate_template()` 可直接按配置生成 Sheet。
- `_fill_form()`、`_click_query_and_wait()`、`_export_and_save()` 都能复用同一套流程。

---

## 10. 核对场景的精确命名控制

### 场景
历史数据核对要求网页版 new 报表按 `newsr2003`、`newzc2003`、`newtk2003` 等固定规则命名。如果继续沿用 `{自定义名}_{日期}_{系统原文件名}`，后续一键核对无法直接识别。

### 解决方案

参数表新增两个可选列：

| 列名 | 取值 | 含义 |
|---|---|---|
| `是否追加日期` | `1/0` | 是否在 `文件名称` 后追加日期 |
| `保留原文件名` | `1/0` | 是否继续拼接系统导出的原始文件名 |

核对专用参数表将两列都设为 `0`，导出文件会精确保存为 `文件名称 + 扩展名`。
