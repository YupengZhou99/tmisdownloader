"""Tk task-management UI. Browser operations live on the service thread."""

import os
from pathlib import Path
import queue
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from tmis_queue import QueueStore, STATUS_LABELS, RETRYABLE
from tmis_runtime import redact_urls, validate_login_url
from tmis_service import QueueService


class QueueWindow:
    BG_PRIMARY = '#1a1a2e'
    BG_CARD = '#16213e'
    BG_INPUT = '#0f3460'
    BG_LOG = '#0d1117'
    FG_PRIMARY = '#e8e8e8'
    FG_SECONDARY = '#a6b1c4'
    CLR_BLUE = '#3268a8'
    CLR_GREEN = '#238766'

    def __init__(self, state_dir=None, service_factory=QueueService):
        super().__init__()
        self.title('TMIS 数据自由查询 · v5.3 队列版')
        self.geometry('1180x820')
        self.minsize(960, 700)
        self.configure(bg=self.BG_PRIMARY)
        self._closing = False
        self._closed_store = False
        self.ui_actions = queue.Queue()
        self.selected_files = []
        self._import_submitted = set()
        self._state = {'mode': 'idle', 'session_ready': False, 'current': None, 'importing': False}
        self._rows = {}
        self.excel_path = tk.StringVar(value='尚未选择参数表；也可以先登录')
        self.download_folder = tk.StringVar()
        self.start_date_var = tk.StringVar()
        self.end_date_var = tk.StringVar()
        self.chrome_path_var = tk.StringVar()
        self.enable_postprocess = tk.BooleanVar(value=False)
        self.naming_mode = tk.StringVar(value='param')
        self.summary_var = tk.StringVar()
        self.session_var = tk.StringVar(value='会话：未登录  |  队列：空闲')
        self.sniper_thread = None
        self._build_gui()
        try:
            self.store = QueueStore(state_dir)
            self.service = service_factory(self.store, self.report_configs, self.column_mapping, self.engine_class)
        except Exception:
            if hasattr(self, 'store'):
                self.store.close()
            self.destroy()
            raise
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self._refresh_tables()
        restored = self.store.counts()
        if restored:
            self.log('已恢复本机任务记录；成功项不会重跑，重新登录后可继续未完成任务或重试失败项')
        self.log('可先登录，也可先添加参数表；每份参数表会使用独立下载子目录')
        self.log('任务结束仅进入空闲；退出程序才关闭浏览器。登录链接不写入任务库。')
        self.log('任务库：' + str(self.store.directory))
        self._poll_id = self.after(100, self._poll)

    def _button(self, parent, text, command, color=None, **kwargs):
        return tk.Button(parent, text=text, command=command, bg=color or self.BG_INPUT,
                         fg='white', activebackground='#245c88', activeforeground='white',
                         relief='flat', padx=12, pady=6, highlightthickness=0, **kwargs)

    def _build_gui(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('Main.TFrame', background=self.BG_PRIMARY)
        style.configure('Card.TFrame', background=self.BG_CARD)
        style.configure('Card.TLabel', background=self.BG_CARD, foreground=self.FG_SECONDARY)
        style.configure('Status.TLabel', background=self.BG_PRIMARY, foreground=self.FG_SECONDARY)
        style.configure('Queue.Treeview', background=self.BG_LOG, fieldbackground=self.BG_LOG,
                        foreground=self.FG_PRIMARY, rowheight=27, borderwidth=0)
        style.configure('Queue.Treeview.Heading', background=self.BG_INPUT, foreground='white', padding=6)
        style.map('Queue.Treeview', background=[('selected', '#285279')], foreground=[('selected', '#ffffff')])
        style.configure('TNotebook', background=self.BG_PRIMARY, borderwidth=0)
        style.configure('TNotebook.Tab', padding=(16, 7))
        outer = ttk.Frame(self, style='Main.TFrame', padding=(18, 14))
        outer.pack(fill='both', expand=True)
        tk.Label(outer, text='TMIS  数据自由查询任务中心', font=('TkDefaultFont', 17, 'bold'),
                 bg=self.BG_PRIMARY, fg='#8edcf2').pack(anchor='w', pady=(0, 10))
        card = ttk.Frame(outer, style='Card.TFrame', padding=12)
        card.pack(fill='x')
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text='待加入参数表', style='Card.TLabel').grid(row=0, column=0, sticky='w', padx=(0, 10))
        ttk.Entry(card, textvariable=self.excel_path, state='readonly').grid(row=0, column=1, sticky='ew', padx=(0, 8))
        self.select_btn = self._button(card, '选择（多选）', self._select_excel)
        self.select_btn.grid(row=0, column=2, padx=(0, 8))
        self.import_btn = self._button(card, '加入队列', self._enqueue_selected)
        self.import_btn.grid(row=0, column=3)
        ttk.Label(card, text='保存根目录', style='Card.TLabel').grid(row=1, column=0, sticky='w', pady=8)
        ttk.Entry(card, textvariable=self.download_folder).grid(row=1, column=1, sticky='ew', padx=(0, 8))
        self._button(card, '选择目录', self._select_folder).grid(row=1, column=2, sticky='ew')
        ttk.Label(card, text='各表独立子目录', style='Card.TLabel').grid(row=1, column=3, padx=(8, 0))
        settings = ttk.Frame(card, style='Card.TFrame')
        settings.grid(row=2, column=0, columnspan=4, sticky='ew', pady=(0, 8))
        for label, variable in [('后备起始日期', self.start_date_var), ('后备终止日期', self.end_date_var)]:
            ttk.Label(settings, text=label, style='Card.TLabel').pack(side='left', padx=(0, 6))
            ttk.Entry(settings, textvariable=variable, width=12).pack(side='left', padx=(0, 14))
        ttk.Label(settings, text='表内日期优先；选项仅作用于新加入的批次', style='Card.TLabel').pack(side='left')
        ttk.Label(card, text='浏览器路径', style='Card.TLabel').grid(row=3, column=0, sticky='w')
        ttk.Entry(card, textvariable=self.chrome_path_var).grid(row=3, column=1, sticky='ew', padx=(0, 8))
        ttk.Label(card, text='留空自动检测 / 麒麟随包浏览器', style='Card.TLabel').grid(row=3, column=2, columnspan=2, sticky='w')
        options = ttk.Frame(outer, style='Main.TFrame')
        options.pack(fill='x', pady=8)
        for text, value in [('参数表命名', 'param'), ('自动命名', 'auto')]:
            tk.Radiobutton(options, text=text, value=value, variable=self.naming_mode,
                           bg=self.BG_PRIMARY, fg=self.FG_PRIMARY, selectcolor=self.BG_INPUT,
                           activebackground=self.BG_PRIMARY, activeforeground='white').pack(side='left', padx=(0, 12))
        tk.Checkbutton(options, text='启用数据后处理（默认关闭）', variable=self.enable_postprocess,
                       bg=self.BG_PRIMARY, fg=self.FG_SECONDARY, selectcolor=self.BG_INPUT,
                       activebackground=self.BG_PRIMARY, activeforeground='white').pack(side='left')
        self._button(options, '清空待选文件', self._clear_selected).pack(side='right')
        bar = ttk.Frame(outer, style='Main.TFrame')
        bar.pack(fill='x')
        self.login_btn = self._button(bar, '粘贴链接并登录', self._paste_login_url, self.CLR_BLUE)
        self.login_btn.pack(side='left', padx=(0, 8))
        self.start_btn = self._button(bar, '开始 / 继续', self._start_task, self.CLR_GREEN)
        self.start_btn.pack(side='left', padx=(0, 8))
        self.stop_btn = self._button(bar, '暂停（当前条完成后）', self._stop_task, state='disabled')
        self.stop_btn.pack(side='left', padx=(0, 8))
        self.sniper_btn = self._button(bar, '捕获登录链接', self._start_sniper)
        if not sys.platform.startswith('linux'):
            self.sniper_btn.pack(side='left', padx=(0, 8))
        self._button(bar, '退出程序', self._on_close).pack(side='right')
        ttk.Label(outer, textvariable=self.session_var, style='Status.TLabel').pack(anchor='w', pady=(10, 3))
        ttk.Label(outer, textvariable=self.summary_var, style='Status.TLabel').pack(anchor='w', pady=(0, 8))
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill='both', expand=True)
        self.queue_tab = ttk.Frame(self.notebook, style='Main.TFrame')
        self.failure_tab = ttk.Frame(self.notebook, style='Main.TFrame')
        self.batch_tab = ttk.Frame(self.notebook, style='Main.TFrame')
        self.log_tab = ttk.Frame(self.notebook, style='Main.TFrame')
        for tab, name in [(self.queue_tab, '任务队列'), (self.failure_tab, '失败 / 超时 / 中断'), (self.batch_tab, '参数表批次'), (self.log_tab, '运行日志')]:
            self.notebook.add(tab, text=name)
        for tab, failures in [(self.queue_tab, False), (self.failure_tab, True)]:
            actions = ttk.Frame(tab, style='Main.TFrame', padding=(0, 8))
            actions.pack(fill='x')
            self._button(actions, '重试选中', lambda f=failures: self._retry_selected(f)).pack(side='left', padx=(0, 8))
            if failures:
                self._button(actions, '全部失败重试', self._retry_all).pack(side='left', padx=(0, 8))
            else:
                self._button(actions, '移除选中待执行项', self._remove_selected).pack(side='left', padx=(0, 8))
            self._button(actions, '查看任务 / 重试记录', lambda f=failures: self._show_task(f)).pack(side='left')
        columns = [('source', '参数表', 170), ('sheet', '工作表', 90), ('row', '行号', 48), ('kind', '类型', 48),
                   ('treasury', '国库', 98), ('dates', '起止日期', 160), ('status', '状态', 90),
                   ('attempts', '次数', 46), ('stage', '阶段', 100), ('error', '错误 / 原因', 340)]
        self.task_tree = self._tree(self.queue_tab, columns)
        self.failure_tree = self._tree(self.failure_tab, columns)
        self.batch_tree = self._tree(self.batch_tab, [('source', '参数表', 250), ('total', '任务数', 70), ('success', '成功', 70), ('failed', '失败/超时', 90), ('pending', '待执行/中断', 100), ('directory', '下载子目录', 580)])
        self.task_tree.bind('<Double-1>', lambda event: self._show_task(False))
        self.failure_tree.bind('<Double-1>', lambda event: self._show_task(True))
        self.log_text = tk.Text(self.log_tab, bg=self.BG_LOG, fg='#b3d9c6', relief='flat', wrap='word', state='disabled', padx=10, pady=10)
        scrollbar = ttk.Scrollbar(self.log_tab, command=self.log_text.yview)
        scrollbar.pack(side='right', fill='y')
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(fill='both', expand=True)
        for level, color in [('ERROR', '#ff9292'), ('WARN', '#efd48a'), ('SUCCESS', '#91d8b5')]:
            self.log_text.tag_configure(level, foreground=color)

    def _tree(self, parent, columns):
        frame = ttk.Frame(parent, style='Main.TFrame')
        frame.pack(fill='both', expand=True)
        tree = ttk.Treeview(frame, columns=[x[0] for x in columns], show='headings', selectmode='extended', style='Queue.Treeview')
        for key, label, width in columns:
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=40, stretch=False)
        vertical = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        for tag, color in [('failed', '#ff9a9a'), ('timed_out', '#efd48a'), ('interrupted', '#efd48a'), ('succeeded', '#91d8b5'), ('running', '#8edcf2')]:
            tree.tag_configure(tag, foreground=color)
        return tree

    def post_ui(self, callback):
        self.ui_actions.put(callback)

    def log(self, message, level='INFO'):
        # Also safe for legacy capture callbacks: actual Tk writes happen in _poll.
        text = redact_urls(message)
        self.ui_actions.put(lambda: self._append_log(text, level))

    def _append_log(self, message, level):
        from datetime import datetime
        self.log_text.configure(state='normal')
        self.log_text.insert('end', f'[{datetime.now():%H:%M:%S}] {message}\n', level)
        if int(self.log_text.index('end-1c').split('.')[0]) > 5000:
            self.log_text.delete('1.0', '1000.0')
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    def _snapshot_run_options(self):
        return {'start_date': self.start_date_var.get().strip(), 'end_date': self.end_date_var.get().strip(),
                'naming_mode': self.naming_mode.get(), 'postprocess': self.enable_postprocess.get()}

    def _selected_label(self):
        names = [Path(p).name for p in self.selected_files]
        self.excel_path.set(('；'.join(names[:2]) + (f' 等 {len(names)} 份' if len(names) > 2 else '')) if names else '可继续多选添加参数表；已入队的文件见下方批次列表')

    def _select_excel(self):
        paths = filedialog.askopenfilenames(parent=self, title='选择参数表（可多选；运行中也可追加）', filetypes=[('Excel 参数表', '*.xlsx *.xls')])
        self.selected_files.extend(p for p in paths if p not in self.selected_files)
        self._selected_label()

    def _select_folder(self):
        folder = filedialog.askdirectory(parent=self, title='选择下载根目录，每份参数表会新建独立子目录')
        if folder:
            self.download_folder.set(folder)

    def _clear_selected(self):
        self.selected_files = [p for p in self.selected_files if p in self._import_submitted]
        self._selected_label()

    def _command(self, command, *args, **kwargs):
        if self._closing:
            return False
        try:
            self.service.submit(command, *args, **kwargs)
            return True
        except Exception as error:
            messagebox.showerror('无法执行', str(error), parent=self)
            return False

    def _enqueue_selected(self, start_after=False):
        paths = [p for p in self.selected_files if p not in self._import_submitted]
        if not paths:
            if start_after:
                self._command('start')
            else:
                self.log('请先多选参数表；导入中的文件不会重复提交', 'WARN')
            return
        if not Path(self.download_folder.get()).is_dir() or not self.download_folder.get():
            self._select_folder()
        root = self.download_folder.get()
        if not root or not Path(root).is_dir():
            return
        self._import_submitted.update(paths)
        if not self._command('import_files', paths, root, self._snapshot_run_options(), start_after=start_after):
            self._import_submitted.difference_update(paths)

    def _paste_login_url(self, value=None):
        if value is None:
            value = simpledialog.askstring('登录 TMIS', '粘贴本次有效的完整登录 URL（仅在内存中使用）：', parent=self, show='*')
        if value is None:
            return
        try:
            url = validate_login_url(value)
        except ValueError as error:
            messagebox.showerror('登录链接无效', str(error), parent=self)
            return
        self._command('login', url, self.chrome_path_var.get().strip())

    def _start_task(self):
        self._enqueue_selected(start_after=True)

    def _stop_task(self):
        self._command('pause')

    def _retry_selected(self, failures=False):
        tree = self.failure_tree if failures else self.task_tree
        ids = [i for i in tree.selection() if self._rows.get(i, {}).get('status') in RETRYABLE]
        if not ids:
            messagebox.showinfo('重试', '请选中失败、超时或中断任务；成功任务不会重跑。', parent=self)
            return
        if messagebox.askyesno('重试选中', f'将 {len(ids)} 条任务加入队尾并开始/继续队列？\n其他待执行项仍按队列顺序执行，成功项不重跑。', parent=self):
            self._command('retry', ids)

    def _retry_all(self):
        count = sum(row['status'] in ('failed', 'timed_out') for row in self._rows.values())
        if count and messagebox.askyesno('全部失败重试', f'将全部 {count} 条失败/超时任务加入队尾并开始/继续？', parent=self):
            self._command('retry')

    def _remove_selected(self):
        ids = list(self.task_tree.selection())
        if ids and messagebox.askyesno('移除待执行项', '只移除选中的待执行/中断项，当前任务和成功记录不受影响。继续？', parent=self):
            self._command('remove', ids)

    def _show_task(self, failures=False):
        tree = self.failure_tree if failures else self.task_tree
        selected = tree.selection()
        if not selected:
            return
        row = self._rows[selected[0]]
        dialog = tk.Toplevel(self)
        dialog.title('任务详情 / 历次尝试')
        dialog.geometry('900x570')
        box = tk.Text(dialog, wrap='word', padx=12, pady=12)
        scrollbar = ttk.Scrollbar(dialog, command=box.yview)
        scrollbar.pack(side='right', fill='y')
        box.configure(yscrollcommand=scrollbar.set)
        box.pack(fill='both', expand=True)
        lines = [f"来源：{row['source']}", f"工作表：{row['sheet']}  第 {row['row_number']} 行", f"类型：{row['kind']}  状态：{STATUS_LABELS[row['status']]}", f"文件名前缀：{row['output_name']}", f"下载目录：{row['output_dir']}", f"已保存文件：{row['saved_path']}", '', '固定查询参数：']
        lines += [f'{key}：{value}' for key, value in row['params'].items()]
        lines += ['', '历次尝试：']
        for attempt in self.store.history(row['id']):
            lines.append(f"第 {attempt['number']} 次 | {STATUS_LABELS[attempt['status']]} | {attempt['stage']} | {attempt['started']} → {attempt['finished']}\n{attempt['error']}")
        box.insert('1.0', '\n'.join(lines))
        box.configure(state='disabled')

    def _sync_tree(self, tree, rows):
        wanted = set()
        for task_id, values, tag in rows:
            wanted.add(task_id)
            if tree.exists(task_id):
                if tuple(tree.item(task_id, 'values')) != tuple(str(v) for v in values):
                    tree.item(task_id, values=values, tags=(tag,))
                tree.move(task_id, '', 'end')
            else:
                tree.insert('', 'end', iid=task_id, values=values, tags=(tag,))
        for task_id in set(tree.get_children()) - wanted:
            tree.delete(task_id)

    def _refresh_tables(self):
        tasks = self.store.tasks()
        self._rows = {r['id']: r for r in tasks}
        rendered, failures = [], []
        for row in tasks:
            p = row['params']
            values = (row['source_name'], row['sheet'], row['row_number'], row['kind'], p.get('pTreCode', ''), f"{p.get('pStartDate', '')}—{p.get('pEndDate', '')}", STATUS_LABELS[row['status']], row['attempt_count'], row['stage'], row['error'])
            item = (row['id'], values, row['status'])
            rendered.append(item)
            if row['status'] in RETRYABLE:
                failures.append(item)
        self._sync_tree(self.task_tree, rendered)
        self._sync_tree(self.failure_tree, failures)
        self._sync_tree(self.batch_tree, [(b['id'], (b['source_name'], b['total'], b['succeeded'], b['failed'], b['pending'], b['output_dir']), '') for b in self.store.batches()])
        counts = self.store.counts()
        self.summary_var.set(f"任务 {len(tasks)}  |  待执行 {counts.get('pending', 0)}  |  执行中 {counts.get('running', 0)}  |  成功 {counts.get('succeeded', 0)}  |  失败/超时 {counts.get('failed', 0) + counts.get('timed_out', 0)}  |  中断 {counts.get('interrupted', 0)}  |  已移除 {counts.get('cancelled', 0)}")

    def _update_status(self, state):
        self._state = state
        labels = {'idle': '空闲待命', 'running': '运行中', 'pausing': '当前条完成后暂停', 'paused': '已暂停', 'waiting_login': '等待重新登录', 'logging_in': '登录中'}
        self.session_var.set('会话：' + ('已登录' if state['session_ready'] else '未就绪') + '  |  队列：' + labels.get(state['mode'], state['mode']) + ('  |  正在导入参数表' if state.get('importing') else ''))
        busy = bool(state.get('current')) or state['mode'] in ('logging_in', 'running', 'pausing')
        self.login_btn.configure(state='disabled' if busy else 'normal')
        self.sniper_btn.configure(state='disabled' if busy else 'normal')
        self.start_btn.configure(state='disabled' if state['mode'] in ('running', 'pausing') else 'normal')
        self.stop_btn.configure(state='normal' if state['mode'] in ('running', 'pausing', 'waiting_login', 'logging_in') else 'disabled')

    def _poll(self):
        changed = False
        while True:
            try:
                self.ui_actions.get_nowait()()
            except queue.Empty:
                break
        while True:
            try:
                event = self.service.events.get_nowait()
            except queue.Empty:
                break
            kind = event['event']
            if kind == 'log':
                self._append_log(event['text'], event['level'])
            elif kind == 'changed':
                changed = True
            elif kind == 'status':
                self._update_status(event)
            elif kind == 'completed':
                changed = True
                failed = event['counts'].get('failed', 0) + event['counts'].get('timed_out', 0)
                self._append_log(f"本轮结束：成功 {event['counts'].get('succeeded', 0)}，失败/超时 {failed}。浏览器保持开启。", 'WARN' if failed else 'SUCCESS')
                if failed:
                    self.notebook.select(self.failure_tab)
            elif kind == 'imported':
                attempted = event['accepted'] + event['duplicates'] + [r['path'] for r in event['errors']]
                self._import_submitted.difference_update(attempted)
                self.selected_files = [p for p in self.selected_files if p not in event['accepted']]
                self._selected_label()
                changed = True
                if event['errors'] and not self._closing:
                    messagebox.showwarning('部分参数表未加入', '\n'.join(f"{Path(r['path']).name}：{r['error']}" for r in event['errors']), parent=self)
                if event['duplicates'] and not self._closing:
                    names = '\n'.join(Path(p).name for p in event['duplicates'])
                    if messagebox.askyesno('重复导入确认', f'{names}\n\n相同任务已导入过。失败项可直接重试。\n仍要在新的子目录中另建批次、再次下载吗？', parent=self):
                        self._import_submitted.update(event['duplicates'])
                        self._command('import_files', event['duplicates'], event['root'], event['options'], allow_duplicate=True, start_after=event['start_after'])
            elif kind in ('error', 'fatal') and not self._closing:
                self._append_log(event['message'], 'ERROR')
                messagebox.showerror('操作未完成', event['message'], parent=self)
        if changed and not self._closed_store:
            self._refresh_tables()
        if self._closing and not self.service.thread.is_alive():
            self.store.close()
            self._closed_store = True
            self.destroy()
            return
        self._poll_id = self.after(100, self._poll)

    def _on_close(self):
        if self._closing:
            return
        if messagebox.askyesno('退出程序', '退出将关闭本程序的浏览器和后台服务。\n未完成/失败任务已保存在本机，下次需重新登录。\n如果只是暂停，请取消并使用“暂停”。确定退出？', parent=self):
            self._closing = True
            self.session_var.set('正在保存状态并关闭浏览器，请稍候…')
            self.service.shutdown()
