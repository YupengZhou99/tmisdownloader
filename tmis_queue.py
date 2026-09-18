"""Durable, credential-free task queue. No GUI or browser ownership here."""

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timezone
from uuid import uuid4

import pandas as pd

from tmis_runtime import normalize_task_row, redact_urls


STATUS_LABELS = {
    'pending': '待执行', 'running': '执行中', 'succeeded': '成功',
    'failed': '失败', 'timed_out': '超时', 'interrupted': '中断待恢复',
    'cancelled': '已移除', 'waiting_login': '等待重新登录',
}
RETRYABLE = ('failed', 'timed_out', 'interrupted')
OPTION_KEYS = ('start_date', 'end_date', 'naming_mode', 'postprocess')


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def default_state_dir():
    if os.environ.get('TMIS_STATE_DIR'):
        return Path(os.environ['TMIS_STATE_DIR']).expanduser().resolve()
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/TMISDLer'
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'TMISDLer'
    return Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'tmis-dler'


def safe_component(value, limit=100):
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(value)).strip(' .')
    return (text[:limit].rstrip(' .') or '参数表')


def parse_parameter_file(filename, configs, mapping, options, engine_factory):
    """Freeze one workbook atomically; invalid rows reject that file, not others."""
    source = Path(filename).expanduser().resolve()
    if source.stat().st_size > 50 * 1024 * 1024:
        raise ValueError('参数表超过 50 MB，请拆分后导入')
    content = source.read_bytes()
    if len(content) > 50 * 1024 * 1024:
        raise ValueError('参数表超过 50 MB，请拆分后导入')
    options = {k: options[k] for k in OPTION_KEYS}
    if any(re.search(r'https?://|\b(?:token|authorization|cookie)\s*[:=]', str(v), re.I) for v in options.values()):
        raise ValueError('任务选项不能包含登录凭据')
    if not isinstance(options['postprocess'], bool):
        raise ValueError('后处理选项必须为开关值')
    if options['naming_mode'] not in ('param', 'auto'):
        raise ValueError('请选择参数表命名或自动命名')
    try:
        sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, dtype=str)
    except ImportError as error:
        raise ValueError('无法读取此 Excel 格式；请另存为 .xlsx 后导入') from error
    engine = engine_factory()
    engine.run_options = options
    engine.log = lambda *args, **kwargs: None
    names, counter, rows = set(), {}, []
    allowed = {'文件名称', '是否追加日期', '保留原文件名'}
    for config in configs.values():
        for category in ('dropdown_fields', 'date_fields', 'text_fields', 'checkbox_fields'):
            allowed.update(config[category])
    for sheet_name, frame in sheets.items():
        inferred = next((kind for kind in configs if kind in sheet_name), None)
        frame.columns = [str(c).strip() for c in frame.columns]
        if not inferred and not {'收支类型', '数据类型'}.intersection(frame.columns):
            continue
        mapped_columns = [mapping.get(c, c) for c in frame.columns]
        if any(re.match(r'^(.+)\.\d+$', c) and c.rsplit('.', 1)[0] in frame.columns
               and mapping.get(c.rsplit('.', 1)[0], c.rsplit('.', 1)[0]) in allowed
               for c in frame.columns):
            raise ValueError(f'{sheet_name}：Excel 中有重复字段表头，请删除重复列后导入')
        if len(mapped_columns) != len(set(mapped_columns)):
            raise ValueError(f'{sheet_name}：重复表头或同义表头映射到同一字段')
        frame.columns = mapped_columns
        for offset, (_, raw) in enumerate(frame.fillna('').iterrows(), 2):
            if not any(str(v).strip() for v in raw):
                continue
            try:
                declared = str(raw.get('收支类型', raw.get('数据类型', ''))).strip()
                kind = declared or inferred
                if kind not in configs:
                    raise ValueError('无法识别报表类型，请检查工作表名称或数据类型列')
                values = {key: str(value).strip() for key, value in raw.items() if key in allowed}
                # Authentication is never a task parameter or durable setting.
                if any(re.search(r'https?://|\b(?:token|authorization|cookie)\s*[:=]', value, re.I) for value in values.values()):
                    raise ValueError('参数行不能包含登录链接、token 或 Cookie')
                for field, fallback in (('pStartDate', 'start_date'), ('pEndDate', 'end_date')):
                    values[field] = values.get(field) or options[fallback]
                values = dict(normalize_task_row(values))
                if not values['pStartDate'] or not values['pEndDate']:
                    raise ValueError('请在参数表或界面中填写起止日期')
                for field in configs[kind]['checkbox_fields']:
                    if values.get(field, '') not in ('', '0', '1'):
                        raise ValueError(f'{field}只能填写 0 或 1')
                name = engine._build_filename(values, kind, counter)
                if not name or name in ('.', '..') or name != os.path.basename(name) or len(name.encode('utf-8')) > 200:
                    raise ValueError('输出文件名前缀无效或过长（最多 200 UTF-8 字节）')
                if name.casefold() in names:
                    raise ValueError(f'输出文件名重复：{name}；请修改文件名称列或选择自动命名')
                names.add(name.casefold())
                rows.append({'kind': kind, 'sheet': sheet_name, 'row_number': offset,
                             'params': values, 'output_name': name})
            except Exception as error:
                raise ValueError(f'{sheet_name} 第 {offset} 行：{redact_urls(error)}') from error
    if not rows:
        raise ValueError('未发现收入、支出、退库或库存任务，请检查工作表名和表头')
    if len(rows) > 50000:
        raise ValueError('单个参数表超过 50000 条，请拆分导入')
    frozen = json.dumps({'rows': rows, 'options': options}, ensure_ascii=False, sort_keys=True)
    return {'source': str(source), 'source_name': source.name, 'rows': rows, 'options': options,
            'file_hash': hashlib.sha256(content).hexdigest(),
            'fingerprint': hashlib.sha256(frozen.encode('utf-8')).hexdigest()}


class DuplicateBatch(ValueError):
    pass


class QueueStore:
    """Thread-safe transactions + a process lock prevent concurrent consumers."""

    def __init__(self, directory=None, backup_legacy=False):
        self.directory = Path(directory) if directory is not None else default_state_dir()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.closed = False
        self.connection = None
        self.lock_file = (self.directory / 'queue.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.lock_file.seek(0)
                self.lock_file.write(b'0')
                self.lock_file.flush()
                self.lock_file.seek(0)
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.lock_file.close()
            raise RuntimeError('此任务库已被另一个程序占用，请关闭另一个窗口后再启动') from error
        try:
            self.connection = sqlite3.connect(str(self.directory / 'queue.sqlite3'), timeout=15, check_same_thread=False)
            # Hold the same queue lock before taking a consistent SQLite backup,
            # including WAL contents, and before recovering interrupted tasks.
            backup = self.directory / 'queue.pre-workspaces.sqlite3'
            if backup_legacy and not backup.exists() and self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
                temporary = self.directory / ('queue.backup-' + uuid4().hex + '.sqlite3')
                try:
                    with sqlite3.connect(str(temporary)) as target:
                        self.connection.backup(target)
                    temporary.chmod(0o600)
                    os.replace(str(temporary), str(backup))
                finally:
                    if temporary.exists():
                        temporary.unlink()
            self.connection.row_factory = sqlite3.Row
            self.connection.execute('PRAGMA foreign_keys=ON')
            self.connection.execute('PRAGMA journal_mode=WAL')
            self.connection.execute('PRAGMA synchronous=FULL')
            version = self.connection.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError('任务库来自更新版本，不能用旧程序打开；原数据未重建')
            self.connection.executescript('''
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, source_name TEXT NOT NULL,
                    file_hash TEXT NOT NULL, fingerprint TEXT NOT NULL, output_dir TEXT NOT NULL,
                    options TEXT NOT NULL, created TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS batch_fingerprint ON batches(fingerprint);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
                    position INTEGER NOT NULL, kind TEXT NOT NULL, sheet TEXT NOT NULL,
                    row_number INTEGER NOT NULL, params TEXT NOT NULL, output_name TEXT NOT NULL,
                    status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    saved_path TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS task_queue ON tasks(status, position);
                CREATE INDEX IF NOT EXISTS task_batch_position ON tasks(batch_id, position);
                CREATE INDEX IF NOT EXISTS task_position ON tasks(position);
                CREATE TABLE IF NOT EXISTS attempts (
                    task_id TEXT NOT NULL REFERENCES tasks(id), number INTEGER NOT NULL,
                    started TEXT NOT NULL, finished TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    saved_path TEXT NOT NULL DEFAULT '', PRIMARY KEY(task_id, number)
                );
                CREATE TABLE IF NOT EXISTS cleared_tasks (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id), cleared TEXT NOT NULL
                );
                PRAGMA user_version=1;
            ''')
            with self.connection:
                reason = '上次程序在任务执行中退出；请重新登录后继续，已有文件不会被覆盖'
                self.connection.execute("UPDATE attempts SET status='interrupted',finished=?,error=? WHERE status='running'", (utc_now(), reason))
                self.connection.execute("UPDATE tasks SET status='interrupted',error=?,updated=? WHERE status='running'", (reason, utc_now()))
        except Exception:
            self.close()
            raise

    def add_batch(self, plan, output_root, allow_duplicate=False):
        root = Path(output_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError('保存根目录不存在，请重新选择')
        with self.lock, self.connection:
            if not allow_duplicate and self.connection.execute('SELECT 1 FROM batches WHERE fingerprint=?', (plan['fingerprint'],)).fetchone():
                raise DuplicateBatch('相同参数及选项曾经导入；失败任务请使用重试，确需重复下载请确认另建批次')
            batch_id = uuid4().hex
            folder = root / (safe_component(Path(plan['source_name']).stem, 60) + '_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + batch_id[:8])
            folder.mkdir(mode=0o700)
            self.connection.execute('INSERT INTO batches VALUES(?,?,?,?,?,?,?,?)',
                                    (batch_id, plan['source'], plan['source_name'], plan['file_hash'], plan['fingerprint'], str(folder), json.dumps(plan['options'], ensure_ascii=False), utc_now()))
            position = self.connection.execute('SELECT COALESCE(MAX(position),0) FROM tasks').fetchone()[0]
            for offset, row in enumerate(plan['rows'], 1):
                self.connection.execute('INSERT INTO tasks(id,batch_id,position,kind,sheet,row_number,params,output_name,status,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                                        (uuid4().hex, batch_id, position + offset, row['kind'], row['sheet'], row['row_number'], json.dumps(row['params'], ensure_ascii=False), row['output_name'], 'pending', utc_now()))
        return batch_id

    def tasks(self, batch_id=None, task_id=None):
        clauses, values = [], []
        for field, value in (('batch_id', batch_id), ('id', task_id)):
            if value is not None:
                clauses.append('t.' + field + '=?')
                values.append(value)
        with self.lock:
            result = self.connection.execute('''SELECT t.*, b.source, b.source_name, b.output_dir, b.options
                FROM tasks t JOIN batches b ON t.batch_id=b.id'''
                + (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY t.position', values).fetchall()
            return [self._decode(row) for row in result]

    def task_refs(self, statuses):
        # Queue controls need IDs, not every historical row's parameter JSON.
        with self.lock:
            return [dict(row) for row in self.connection.execute(
                'SELECT id,batch_id FROM tasks WHERE status IN (' + ','.join('?' for _ in statuses) + ')', statuses)]

    def task_page(self, offset=0, limit=60, search='', kind='', batch='', failed=False, task_id=''):
        if (type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200
                or type(failed) is not bool or any(not isinstance(v, str) or len(v) > 500 for v in (search, kind, batch, task_id))):
            raise ValueError('分页参数无效')
        clauses, values = ['c.task_id IS NULL'], []
        for column, value in [('t.kind', kind), ('t.batch_id', batch), ('t.id', task_id)]:
            if value:
                clauses.append(column + '=?')
                values.append(value)
        if failed:
            clauses.append("t.status IN ('failed','timed_out','interrupted')")
        if search:
            clauses.append('(t.output_name LIKE ? OR b.source_name LIKE ? OR t.params LIKE ?)')
            values.extend(['%' + search + '%'] * 3)
        source = ' FROM tasks t JOIN batches b ON t.batch_id=b.id LEFT JOIN cleared_tasks c ON c.task_id=t.id WHERE ' + ' AND '.join(clauses)
        with self.lock:
            total = self.connection.execute('SELECT COUNT(*)' + source, values).fetchone()[0]
            rows = self.connection.execute('SELECT t.*,b.source_name,b.output_dir' + source
                                           + ' ORDER BY t.position LIMIT ? OFFSET ?', values + [limit, offset]).fetchall()
        tasks = []
        for row in rows:
            item = dict(row)
            params = json.loads(item.pop('params'))
            item.update(treasury=params.get('pTreCode', ''), start=params.get('pStartDate', ''), end=params.get('pEndDate', ''))
            tasks.append(item)
        return dict(tasks=tasks, total=total)

    def clear_completed(self):
        # Logical removal is reversible and never touches reports or audit rows.
        with self.lock, self.connection:
            return self.connection.execute("""INSERT OR IGNORE INTO cleared_tasks(task_id,cleared)
                SELECT id,? FROM tasks WHERE status='succeeded'""", (utc_now(),)).rowcount

    def batches(self, limit=None):
        with self.lock:
            return [dict(row) for row in self.connection.execute('''SELECT b.*, COUNT(t.id) AS total,
                SUM(t.status='succeeded') AS succeeded, SUM(t.status IN ('failed','timed_out')) AS failed,
                SUM(t.status IN ('pending','interrupted')) AS pending
                FROM batches b LEFT JOIN tasks t ON b.id=t.batch_id AND NOT EXISTS
                (SELECT 1 FROM cleared_tasks c WHERE c.task_id=t.id)
                GROUP BY b.id ORDER BY b.created DESC,b.rowid DESC'''
                + (' LIMIT ?' if limit else ''), (limit,) if limit else ())]

    @staticmethod
    def _decode(row):
        result = dict(row)
        for key in ('params', 'options'):
            if key in result:
                result[key] = json.loads(result[key])
        return result

    def counts(self):
        with self.lock:
            return {r[0]: r[1] for r in self.connection.execute('''SELECT status,COUNT(*) FROM tasks t
                WHERE NOT EXISTS (SELECT 1 FROM cleared_tasks c WHERE c.task_id=t.id) GROUP BY status''')}

    def claim_next(self):
        with self.lock, self.connection:
            row = self.connection.execute("SELECT id FROM tasks WHERE status='pending' ORDER BY position LIMIT 1").fetchone()
            if row is None:
                return None
            task_id = row[0]
            last_attempt = self.connection.execute('SELECT stage FROM attempts WHERE task_id=? ORDER BY number DESC LIMIT 1', (task_id,)).fetchone()
            self.connection.execute("UPDATE tasks SET status='running',attempt_count=attempt_count+1,stage='准备',error='',updated=? WHERE id=?", (utc_now(), task_id))
            task = self._decode(self.connection.execute('SELECT t.*,b.output_dir,b.options,b.source_name,b.source FROM tasks t JOIN batches b ON t.batch_id=b.id WHERE t.id=?', (task_id,)).fetchone())
            task['previous_stage'] = last_attempt['stage'] if last_attempt else ''
            self.connection.execute('INSERT INTO attempts(task_id,number,started,status,stage) VALUES(?,?,?,?,?)', (task_id, task['attempt_count'], utc_now(), 'running', '准备'))
            return task

    def stage(self, task_id, stage, saved_path=''):
        with self.lock, self.connection:
            self.connection.execute("UPDATE tasks SET stage=?,saved_path=CASE WHEN ?='' THEN saved_path ELSE ? END,updated=? WHERE id=? AND status='running'", (stage, saved_path, saved_path, utc_now(), task_id))
            self.connection.execute("UPDATE attempts SET stage=?,saved_path=CASE WHEN ?='' THEN saved_path ELSE ? END WHERE task_id=? AND status='running'", (stage, saved_path, saved_path, task_id))

    def finish(self, task_id, status, error='', saved_path=''):
        if status not in ('succeeded', 'failed', 'timed_out', 'interrupted', 'waiting_login'):
            raise ValueError('无效的完成状态')
        error = redact_urls(error)[:6000]
        with self.lock, self.connection:
            self.connection.execute("UPDATE attempts SET status=?,finished=?,error=?,saved_path=CASE WHEN ?='' THEN saved_path ELSE ? END WHERE task_id=? AND status='running'", (status, utc_now(), error, saved_path, saved_path, task_id))
            self.connection.execute("UPDATE tasks SET status=?,error=?,saved_path=CASE WHEN ?='' THEN saved_path ELSE ? END,updated=? WHERE id=? AND status='running'", ('pending' if status == 'waiting_login' else status, error, saved_path, saved_path, utc_now(), task_id))

    def resume_interrupted(self):
        with self.lock, self.connection:
            self.connection.execute("UPDATE tasks SET status='pending',updated=? WHERE status='interrupted'", (utc_now(),))

    def retry(self, ids=None):
        with self.lock, self.connection:
            candidates = self.connection.execute("SELECT id FROM tasks WHERE status IN ('failed','timed_out','interrupted') ORDER BY position").fetchall()
            ids = set(ids) if ids is not None else None
            position = self.connection.execute('SELECT COALESCE(MAX(position),0) FROM tasks').fetchone()[0]
            count = 0
            for (task_id,) in candidates:
                if ids is not None and task_id not in ids:
                    continue
                position += 1
                self.connection.execute("UPDATE tasks SET status='pending',position=?,stage='等待重试',updated=? WHERE id=?", (position, utc_now(), task_id))
                count += 1
            return count

    def cancel_pending(self, ids):
        with self.lock, self.connection:
            count = 0
            for task_id in set(ids):
                count += self.connection.execute("UPDATE tasks SET status='cancelled',updated=? WHERE id=? AND status IN ('pending','interrupted')", (utc_now(), task_id)).rowcount
            return count

    def history(self, task_id):
        with self.lock:
            return [dict(row) for row in self.connection.execute('SELECT * FROM attempts WHERE task_id=? ORDER BY number', (task_id,))]

    def export_results(self, batch_id):
        """CSV is a readable projection; SQLite remains the recovery authority."""
        with self.lock:
            batch = self.connection.execute('SELECT output_dir FROM batches WHERE id=?', (batch_id,)).fetchone()
            rows = self.tasks(batch_id)
            attempts = [dict(r) for r in self.connection.execute('SELECT a.*,t.kind,t.sheet,t.row_number FROM attempts a JOIN tasks t ON a.task_id=t.id WHERE t.batch_id=? ORDER BY a.started,a.task_id,a.number', (batch_id,))]
        latest = [['任务ID', '参数表', '工作表', '行号', '类型', '文件名称', '国库选择', '起始日期', '终止日期', '展示范围', '辖属标志', '状态', '尝试次数', '阶段', '错误', '保存路径']]
        for row in rows:
            p = row['params']
            latest.append([row['id'], row['source_name'], row['sheet'], row['row_number'], row['kind'], row['output_name'], p.get('pTreCode', ''), p.get('pStartDate', ''), p.get('pEndDate', ''), p.get('pShowScope', ''), p.get('pGovernFlag', ''), STATUS_LABELS[row['status']], row['attempt_count'], row['stage'], row['error'], row['saved_path']])
        history = [['任务ID', '尝试次数', '类型', '工作表', '行号', '开始时间', '结束时间', '状态', '阶段', '错误', '保存路径']]
        history.extend([r['task_id'], r['number'], r['kind'], r['sheet'], r['row_number'], r['started'], r['finished'], STATUS_LABELS[r['status']], r['stage'], r['error'], r['saved_path']] for r in attempts)
        for name, data in (('下载结果.csv', latest), ('重试记录.csv', history)):
            destination = Path(batch['output_dir']) / name
            fd, temporary = tempfile.mkstemp(prefix='.tmis-results-', suffix='.part', dir=str(destination.parent))
            try:
                with os.fdopen(fd, 'w', encoding='utf-8-sig', newline='') as stream:
                    writer = csv.writer(stream)
                    for values in data:
                        writer.writerow([("'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v) for v in values])
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.connection is not None:
                self.connection.close()
            self.lock_file.close()
