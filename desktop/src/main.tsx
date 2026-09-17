import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Activity, ArrowDownToLine, ArrowLeft, ArrowUpRight, Check, CheckCircle2, ChevronDown,
  ChevronLeft, ChevronRight, CircleAlert, Clock3, FileSpreadsheet, Files, FolderOpen, Grip,
  LayoutDashboard, Link, ListFilter, LoaderCircle, LogOut, Maximize2, Minus, Monitor, MoreHorizontal,
  PanelTop, Pause, Pin, Play, Plus, RefreshCw, Search, Settings2, ShieldCheck, Sparkles, Trash2, X } from 'lucide-react';
import type { State, Task, Preview, Options, Log } from './types';
import './style.css';

const empty: State = { tasks: [], batches: [], counts: {}, mode: 'idle', current: null,
  session_ready: false, headless: false, pending_headless: null, switching: false,
  importing: false, state_dir: '', version: '6.0.0' };
const statusName: Record<string, string> = { pending: '待执行', running: '执行中', succeeded: '已完成',
  failed: '失败', timed_out: '超时', interrupted: '待恢复', cancelled: '已移除' };
const modeName: Record<string, string> = { idle: '服务待命', running: '正在执行', paused: '队列已暂停',
  pausing: '当前条结束后暂停', waiting_login: '等待登录', logging_in: '正在登录', switching: '正在切换浏览器' };
const retryable = (task: Task) => ['failed', 'timed_out', 'interrupted'].includes(task.status);
const date = (value = '') => value.length === 8 ? value.slice(0, 4) + '.' + value.slice(4, 6) + '.' + value.slice(6) : value;
const shortName = (path: string) => path.split(/[\\/]/).pop() || path;
const errorText = (error: unknown) => error instanceof Error ? error.message.replace(/^Error invoking remote method '[^']+': Error: /, '') : String(error);
const compactView = new URLSearchParams(location.search).get('view') === 'compact';
function Badge({ status }: { status: string }) { return <span className={'badge ' + status}><i/>{statusName[status] || status}</span>; }
function App() {
  const api = window.tmis;
  const [state, setState] = useState<State>(empty), [tab, setTab] = useState('queue');
  const [logs, setLogs] = useState<Log[]>([]), [offline, setOffline] = useState(!api), [closing, setClosing] = useState(false);
  const [toast, setToast] = useState(''), [completion, setCompletion] = useState<Record<string, number> | null>(null);
  const [modal, setModal] = useState<'login' | 'import' | 'settings' | null>(null), [busy, setBusy] = useState('');
  const [url, setUrl] = useState(''), [browserPath, setBrowserPath] = useState(''), [top, setTop] = useState(true);
  const [paths, setPaths] = useState<string[]>([]), [root, setRoot] = useState(''), [previews, setPreviews] = useState<Preview[]>([]);
  const [duplicate, setDuplicate] = useState(false), [options, setOptions] = useState<Options>({ start_date: '', end_date: '', naming_mode: 'param', postprocess: false });
  const [search, setSearch] = useState(''), [kind, setKind] = useState(''), [batch, setBatch] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set()), [page, setPage] = useState(0);
  const [detail, setDetail] = useState<any>(null), [dragging, setDragging] = useState(false);
  async function attempt<T>(name: string, action: () => Promise<T>): Promise<T | undefined> {
    setBusy(name);
    try { return await action(); }
    catch (error) { setToast(errorText(error)); }
    finally { setBusy(''); }
  }
  async function command(name: string, data = {}) {
    if (!api) return;
    return attempt(name, async () => {
      const result = await api.call(name, data);
      setState(await api.call<State>('snapshot'));
      return result;
    });
  }
  useEffect(() => {
    if (!api) return;
    void api.call<State>('snapshot').then(s => { setState(s); setLogs(s.logs || []); setTop(s.top !== false); }).catch(e => { setOffline(true); setToast(errorText(e)); });
    return api.subscribe(event => {
      if (event.event === 'snapshot') setState(event.data);
      if (event.event === 'ready') setOffline(false);
      if (event.event === 'error' || event.event === 'fatal') setToast(event.message);
      if (event.event === 'offline') { setOffline(true); setToast(event.message); }
      if (event.event === 'closing') setClosing(true);
      if (event.event === 'window') setTop(event.top);
      if (event.event === 'log') setLogs(items => [...items, { text: event.text, level: event.level, time: new Date().toLocaleTimeString('zh-CN', { hour12: false }) }].slice(-300));
      if (event.event === 'completed') {
        setCompletion(event.counts);
        if (event.counts.failed || event.counts.timed_out) setTab('failed');
      }
    });
  }, []);
  useEffect(() => { setPage(0); setSelected(new Set()); }, [tab, search, kind, batch]);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(''), 10000);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.shiftKey && event.key.toLowerCase() === 'm') {
        event.preventDefault(); void api?.window(compactView ? 'expand' : 'compact');
      }
      if (event.key === 'Escape') {
        if (detail) setDetail(null);
        else if (!busy) { setModal(null); setUrl(''); }
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [api, busy, detail]);
  const count = (name: string) => state.counts[name] || 0;
  const failures = count('failed') + count('timed_out') + count('interrupted');
  const total = state.tasks.length - count('cancelled');
  const finished = count('succeeded') + count('failed') + count('timed_out');
  const progress = total ? Math.round(finished / total * 100) : 0;
  const current = state.tasks.find(t => t.id === state.current);
  const running = ['running', 'pausing'].includes(state.mode);
  const locked = offline || closing;
  const filtered = useMemo(() => state.tasks.filter(task =>
    (tab !== 'failed' || retryable(task)) && (!kind || task.kind === kind) && (!batch || task.batch_id === batch) &&
    (!search || [task.output_name, task.source_name, task.treasury, task.start, statusName[task.status]].join(' ').toLowerCase().includes(search.toLowerCase()))
  ), [state.tasks, tab, kind, batch, search]);
  const pages = Math.max(1, Math.ceil(filtered.length / 60));
  const visible = filtered.slice(Math.min(page, pages - 1) * 60, (Math.min(page, pages - 1) + 1) * 60);
  const selection = state.tasks.filter(t => selected.has(t.id));
  function toggle(id: string) { setSelected(prev => { const next = new Set(prev); next.has(id) ? next.delete(id) : next.add(id); return next; }); }
  async function changeOptions(next: Options) {
    await discardPreview();
    setOptions(next);
  }
  async function discardPreview() {
    const ids = previews.flatMap(p => p.id ? [p.id] : []);
    setPreviews([]); setDuplicate(false);
    if (ids.length) await api?.call('discard_preview', { ids }).catch(() => {});
  }
  async function chooseFiles(dropped?: File[]) {
    if (!api) return;
    const picked = await attempt('files', () => dropped ? api.drop(dropped) : api.files());
    if (picked?.length) {
      await discardPreview();
      setPaths(prev => [...new Set([...prev, ...picked])]); setModal('import');
    }
  }
  async function importPlans() {
    if (!api) return;
    const ids = previews.flatMap(p => p.id ? [p.id] : []);
    const result = await attempt('import', () => api.call<{ accepted: { name: string; count: number }[]; errors: { id: string; error: string }[] }>(
      'import_preview', { ids, root, allow_duplicate: duplicate }));
    if (result) {
      setState(await api.call<State>('snapshot'));
      if (!result.errors.length) {
        setModal(null); setPaths([]); setPreviews([]);
        setToast('已加入 ' + result.accepted.reduce((n, b) => n + b.count, 0) + ' 条任务' + (running ? '，将按顺序执行' : '，点击开始队列即可运行'));
      } else {
        const errors = new Map(result.errors.map(e => [e.id, e.error]));
        setPreviews(prev => prev.filter(p => p.id && errors.has(p.id)).map(p => ({ ...p, error: errors.get(p.id!) })));
        setToast('部分参数表未加入队列；已成功的参数表不会重复导入');
      }
      setCompletion(null);
    }
  }
  const startPause = <button className={'btn ' + (running ? 'soft' : 'primary')} disabled={locked || !!busy || state.mode === 'pausing'}
    onClick={() => { setCompletion(null); void command(running ? 'pause' : 'start'); }}>
    {running ? <Pause size={16}/> : <Play size={16}/>} {running ? (state.mode === 'pausing' ? '等待暂停' : '暂停队列') : '开始 / 继续'}</button>;
  const showWindow = (action: string, value?: boolean) => void attempt('window', async () => api?.window(action, value));
  const modeNotice = state.pending_headless !== null || state.switching;

  if (compactView) return <div className="compact">
    <div className="compact-title"><span className="logo small"><ArrowDownToLine size={17}/></span><b>TMIS 下载助手</b><span className="grow"/>
      <button className={'icon-button nodrag ' + (top ? 'blue' : '')} title={top ? '取消置顶' : '窗口置顶'} aria-label="窗口置顶" onClick={() => showWindow('top', !top)}><Pin size={16}/></button>
      <button className="icon-button nodrag" title="返回工作台" aria-label="返回工作台" onClick={() => showWindow('expand')}><Maximize2 size={16}/></button></div>
    <div className="compact-status"><span className={'live-dot ' + (running ? 'active' : '')}/>{locked ? '服务已停止' : modeName[state.mode]}<span>{progress}%</span></div>
    <div className="compact-task" title={current?.output_name}>{current ? current.output_name : failures ? failures + ' 条任务需要处理' : total ? '当前无任务执行 · 服务保持待命' : '添加参数表，开始下载'}</div>
    <div className="progress"><i style={{ width: progress + '%' }}/></div>
    <div className="compact-metrics"><span>已完成 <b>{count('succeeded')}</b> / {total}</span><span className={failures ? 'amber' : ''}>待处理 <b>{failures}</b></span></div>
    <div className="compact-actions">{startPause}<button className="btn" onClick={() => showWindow('expand')}>返回工作台 <ArrowUpRight size={15}/></button></div>
    <div className="compact-foot">{modeNotice ? '浏览器模式切换中 / 等待当前条结束' : current?.stage || '悬浮窗切换不影响后台任务'}</div>
    {toast && <button className="compact-toast" onClick={() => { setToast(''); showWindow('expand'); }}>{toast}</button>}
  </div>;

  return <div className="shell" onDragOver={event => { if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); setDragging(true); } }}
    onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }}
    onDrop={event => { event.preventDefault(); setDragging(false); if (!locked && !busy) void chooseFiles(Array.from(event.dataTransfer.files)); }}>
    <aside className="sidebar">
      <div className="brand"><span className="logo"><ArrowDownToLine size={24}/></span><div><b>TMIS</b><span>数据工作台</span></div><span className="version">6.0</span></div>
      <div className="sidebar-caption">工作空间</div>
      <nav>
        {[['queue', LayoutDashboard, '下载队列'], ['failed', CircleAlert, '待处理任务'], ['batches', Files, '参数表批次'], ['logs', Activity, '运行日志']].map(([key, Icon, label]) => {
          const I = Icon as typeof Files;
          return <button key={key as string} className={tab === key ? 'nav active' : 'nav'} onClick={() => setTab(key as string)}>
            <I size={18}/>{label as string}{key === 'failed' && failures > 0 && <em>{failures}</em>}</button>;
        })}
      </nav>
      <div className="sidebar-tip"><ShieldCheck size={21}/><b>数据留在本机</b><p>登录链接仅用于当前会话。<br/>下载和任务记录保存在本地。</p></div>
      <div className="sidebar-bottom"><div><span className={'live-dot ' + (!locked ? 'active' : '')}/>{locked ? '服务不可用' : '本地服务已连接'}</div>
        <button className="nav" onClick={() => setModal('settings')}><Settings2 size={17}/>偏好与浏览器</button>
        <button className="nav" onClick={() => showWindow('exit')}><LogOut size={17}/>退出应用</button></div>
    </aside>
    <section className="workspace">
      <header className="topbar"><span>工作空间 <ChevronRight size={14}/> {({ queue: '下载队列', failed: '待处理任务', batches: '参数表批次', logs: '运行日志' } as Record<string, string>)[tab]}</span>
        <div className="topbar-right"><span className="local-label"><ShieldCheck size={14}/> 本机运行</span><button className="btn" onClick={() => showWindow('compact')} title="Ctrl / ⌘ + Shift + M"><PanelTop size={16}/>悬浮窗</button></div></header>
      <main>
        <div className="page-heading"><div><div className="eyebrow">TMIS REPORT WORKSPACE</div><h1>{({ queue: '下载队列', failed: '待处理任务', batches: '参数表批次', logs: '运行日志' } as Record<string, string>)[tab]}</h1>
          <p>让报表下载，有条不紊。{tab === 'queue' ? ' 收入、支出、库存，自由查询一站管理。' : ' 每一次执行都有记录，每一个任务都可追溯。'}</p></div>
          <button className="btn primary" disabled={locked || !!busy} onClick={() => setModal('import')}><Plus size={17}/>添加参数表</button></div>
        {offline && <div className="notice danger"><CircleAlert size={18}/>{api ? '后台服务已停止。请退出后重新打开应用；已保存的任务不会丢失。' : '当前为浏览器外观预览，下载、文件选择和窗口控制仅在桌面应用中可用。'}</div>}
        {closing && <div className="notice"><LoaderCircle className="spin" size={18}/>正在保存任务并关闭浏览器，请稍候…</div>}
        {completion && <div className={'notice ' + (completion.failed || completion.timed_out ? 'warning' : 'success')}><CheckCircle2 size={18}/>
          <span>本轮已结束：成功 {completion.succeeded || 0} 条，失败 / 超时 {(completion.failed || 0) + (completion.timed_out || 0)} 条。浏览器和服务保持开启。</span>
          <button className="icon-button" aria-label="关闭完成提示" onClick={() => setCompletion(null)}><X size={16}/></button></div>}
        <div className="stats">
          {[['全部任务', total, Files, 'neutral', '不含已移除'], ['等待执行', count('pending') + count('running'), Clock3, 'blue', count('running') ? '1 条正在执行' : '按导入顺序执行'],
            ['成功下载', count('succeeded'), CheckCircle2, 'green', '文件已保存到本机'], ['需要处理', failures, CircleAlert, 'amber', '失败、超时及中断']].map(([title, value, Icon, color, note]) => {
            const I = Icon as typeof Files;
            return <div className="stat" key={title as string}><div><span>{title as string}</span><span className={'stat-icon ' + color}><I size={19}/></span></div><strong>{value as number}</strong><small>{note as string}</small></div>;
          })}
        </div>
        <div className="control-card">
          <div className="control-main"><div className={'session-icon ' + (state.session_ready ? 'connected' : '')}><Monitor size={23}/></div>
            <div className="session-text"><b>{state.session_ready ? 'TMIS 会话已就绪' : state.mode === 'logging_in' ? '正在连接 TMIS…' : '先登录，随时开始'}</b>
              <span>{state.session_ready ? (state.headless ? '无头模式 · 浏览器在后台执行' : '有头模式 · 可查看浏览器页面') : '粘贴登录链接即可连接，无需先选择参数表。'}</span></div>
            <button className="btn" disabled={locked || !!state.current || state.switching || state.pending_headless !== null || state.mode === 'logging_in'} onClick={() => setModal('login')}><Link size={15}/>{state.session_ready ? '重新登录' : '连接 TMIS'}</button>
            <button className="icon-button" aria-label="浏览器设置" onClick={() => setModal('settings')}><MoreHorizontal size={20}/></button>
            <span className="vertical-line"/>{startPause}</div>
          <div className="execution-line"><span className={'live-dot ' + (running ? 'active' : '')}/><b>{modeName[state.mode]}</b>
            <span className="execution-name">{current ? current.output_name + ' · ' + current.stage : '可多选或拖入 Excel 参数表，运行中也可追加'}</span><span className="numeric">{finished} / {total} 已处理</span></div>
          <div className="progress"><i style={{ width: progress + '%' }}/></div>
          {modeNotice && <div className="mode-notice"><LoaderCircle size={15} className={state.switching ? 'spin' : ''}/>
            {state.switching ? '正在重建浏览器并检查登录状态，队列暂不领取新任务…' : '已预约切换为' + (state.pending_headless ? '无头模式' : '有头模式') + '，当前任务结束后执行。'}
            {!state.switching && <button className="text-button" onClick={() => void command('cancel_mode')}>取消切换</button>}</div>}
        </div>
        {(tab === 'queue' || tab === 'failed') && <section className="panel">
          <div className="panel-toolbar"><div className="search"><Search size={16}/><input aria-label="搜索任务" placeholder="搜索文件名称、国库代码或日期…" value={search} onChange={e => setSearch(e.target.value)}/></div>
            <select aria-label="报表类型" value={kind} onChange={e => setKind(e.target.value)}><option value="">全部报表</option>{['收入', '支出', '库存', '退库'].map(v => <option key={v}>{v}</option>)}</select>
            <select aria-label="参数批次" className="batch-filter" value={batch} onChange={e => setBatch(e.target.value)}><option value="">全部批次</option>{state.batches.map((b, i) => <option key={b.id} value={b.id}>{i + 1}. {b.source_name}</option>)}</select>
            <span className="grow"/>{tab === 'failed' && <button className="btn" disabled={locked || !!busy || !failures} onClick={() => void command('retry', { ids: state.tasks.filter(retryable).map(t => t.id) })}><RefreshCw size={15}/>重试全部失败 / 中断</button>}
          </div>
          {selected.size > 0 && <div className="selection-bar"><b>已选 {selected.size} 条</b>
            <button className="text-button" disabled={locked || !!busy || !selection.some(retryable)} onClick={() => void command('retry', { ids: selection.filter(retryable).map(t => t.id) })}>重试所选失败 / 中断</button>
            <button className="text-button" disabled={locked || !!busy || !selection.some(t => ['pending', 'interrupted'].includes(t.status))} onClick={() => {
              if (confirm('仅移除选中的待执行 / 中断任务，历史记录保留。是否继续？')) void command('remove', { ids: selection.map(t => t.id) });
            }}>移除待执行任务</button><button className="text-button" onClick={() => setSelected(new Set())}>取消选择</button></div>}
          {filtered.length ? <div className="table-scroll"><table><thead><tr><th className="check-col"><input type="checkbox" aria-label="选择本页任务" checked={visible.length > 0 && visible.every(t => selected.has(t.id))} onChange={() => {
            setSelected(prev => { const next = new Set(prev), all = visible.every(t => prev.has(t.id)); visible.forEach(t => all ? next.delete(t.id) : next.add(t.id)); return next; });
          }}/></th><th>任务 / 来源参数表</th><th>报表</th><th>国库 / 时间范围</th><th>状态</th><th>操作</th></tr></thead><tbody>
            {visible.map(task => <tr key={task.id} className={task.id === state.current ? 'current-row' : ''}><td><input type="checkbox" aria-label={'选择 ' + task.output_name} checked={selected.has(task.id)} onChange={() => toggle(task.id)}/></td>
              <td className="task-cell"><button className="task-name" title={task.output_name} onClick={() => void attempt('details', async () => setDetail(await api!.call('details', { id: task.id })))}>{task.output_name}</button>
                <small title={task.source_name}>{task.source_name} · 第 {task.row_number} 行</small></td><td><span className={'kind kind-' + task.kind}>{task.kind}</span></td>
              <td className="date-cell"><b>{task.treasury || '—'}</b><small>{date(task.start)} — {date(task.end)}</small></td>
              <td><Badge status={task.status}/>{task.status === 'running' && <small className="stage">{task.stage}</small>}</td>
              <td>{retryable(task) ? <button className="row-button" disabled={locked || !!busy} onClick={() => void command('retry', { ids: [task.id] })}><RefreshCw size={14}/>重试</button> :
                <button className="row-button" onClick={() => void attempt('details', async () => setDetail(await api!.call('details', { id: task.id })))}>详情 <ChevronRight size={14}/></button>}</td></tr>)}
          </tbody></table></div> : <div className="empty-state"><span><FileSpreadsheet size={34}/></span><h3>{tab === 'failed' ? '这里没有需要处理的任务' : state.tasks.length ? '没有符合筛选条件的任务' : '把参数表放进来，其余交给工作台'}</h3>
            <p>{tab === 'failed' ? '失败、超时和中断任务会集中显示在这里，可逐条或批量重试。' : '支持收入、支出、库存自由查询 · 多文件导入 · 运行中追加'}</p>
            {tab === 'queue' && !state.tasks.length && <button className="btn" disabled={locked} onClick={() => setModal('import')}><Plus size={16}/>添加第一份参数表</button>}</div>}
          <footer className="table-footer"><span>共 {filtered.length} 条 · 参数与输出路径按导入时锁定</span><div><button className="icon-button" aria-label="上一页" disabled={page === 0} onClick={() => setPage(p => p - 1)}><ChevronLeft size={16}/></button>
            <span>{Math.min(page, pages - 1) + 1} / {pages}</span><button className="icon-button" aria-label="下一页" disabled={page >= pages - 1} onClick={() => setPage(p => p + 1)}><ChevronRight size={16}/></button></div></footer>
        </section>}
        {tab === 'batches' && <section className="panel batch-panel"><div className="panel-title"><h3>已导入的参数表</h3><span>{state.batches.length} 个独立批次</span></div>
          {state.batches.map(b => { const tasks = state.tasks.filter(t => t.batch_id === b.id); return <article className="batch-card" key={b.id}><span className="file-icon"><FileSpreadsheet size={24}/></span>
            <div className="grow"><b>{b.source_name}</b><small>{new Date(b.created).toLocaleString('zh-CN')} · {tasks.length} 条任务 · 成功 {tasks.filter(t => t.status === 'succeeded').length} 条</small>
              <code>{b.output_dir}</code></div><button className="btn" onClick={() => { setTab('queue'); setBatch(b.id); }}>查看任务</button>
            <button className="icon-button" aria-label="打开批次目录" onClick={() => void attempt('open', () => api!.open(b.output_dir))}><FolderOpen size={19}/></button></article>; })}
          {!state.batches.length && <div className="empty-state"><Files size={34}/><h3>尚未导入参数表</h3><p>每份参数表会获得独立输出目录和结果记录。</p></div>}</section>}
        {tab === 'logs' && <section className="panel"><div className="panel-title"><h3>会话日志</h3><span>仅展示本次会话最近 300 条 · 登录链接已脱敏</span></div>
          <div className="logs">{logs.length ? logs.map((log, i) => <div className={'log ' + log.level} key={i}><time>{log.time || '—'}</time><span>{log.level}</span><p>{log.text}</p></div>) :
            <div className="empty-state"><Activity size={30}/><h3>服务已待命</h3><p>登录、查询与下载的进展会显示在这里。</p></div>}</div></section>}
        <footer className="page-footer"><span><ShieldCheck size={14}/> 离线界面 · 无云端上传</span><span>TMIS Workbench / {state.version}</span></footer>
      </main>
    </section>
    {modal && <div className="modal-shade"><section role="dialog" aria-modal="true" aria-labelledby="modal-title" className={'modal ' + (modal === 'import' ? 'wide' : '')}>
      <div className="modal-header"><div><div className="eyebrow">{modal === 'login' ? 'CONNECT' : modal === 'import' ? 'IMPORT & PREVIEW' : 'PREFERENCES'}</div><h2 id="modal-title">{modal === 'login' ? '连接 TMIS' : modal === 'import' ? '添加参数表' : '偏好与浏览器'}</h2></div>
        <button className="icon-button" disabled={!!busy} aria-label="关闭对话框" onClick={() => { setModal(null); setUrl(''); }}><X size={20}/></button></div>
      {modal === 'login' && <form onSubmit={event => { event.preventDefault(); const loginUrl = url; setUrl(''); setModal(null); void command('login', { url: loginUrl, browser_path: browserPath }); }}>
        <div className="modal-body"><div className="notice"><ShieldCheck size={18}/><span>登录链接只保留在当前运行内存中，不写入任务库、设置或日志。</span></div>
          <label>完整登录链接<textarea autoFocus required rows={4} value={url} placeholder="粘贴含登录信息的完整 http:// 或 https:// 链接" onChange={e => setUrl(e.target.value)} autoComplete="off" spellCheck={false}/></label>
          <p className="hint">连接成功后，可以再选择参数表和下载目录。请确保本机可访问 TMIS 内网。</p>
          <div className="setting-row"><div><b>登录时使用{state.headless ? '无头' : '有头'}浏览器</b><small>在“偏好与浏览器”中更改模式</small></div><Monitor size={21}/></div></div>
        <div className="modal-footer"><button type="button" className="btn" onClick={() => { setUrl(''); setModal(null); }}>取消</button><button className="btn primary" disabled={!url.trim() || !!busy || locked}><Link size={16}/>连接工作界面</button></div>
      </form>}
      {modal === 'settings' && <><div className="modal-body">
        <div className="setting-row"><div><b>后台无头模式</b><small>切换需重建浏览器；运行中会等待当前条结束。</small></div>
          <button role="switch" aria-checked={state.pending_headless ?? state.headless} aria-label="后台无头模式" className={'switch ' + ((state.pending_headless ?? state.headless) ? 'on' : '')}
            disabled={locked || state.switching || state.mode === 'logging_in' || !!busy} onClick={() => void command('request_mode', { headless: !(state.pending_headless ?? state.headless) })}><i/></button></div>
        {modeNotice && <div className="notice warning">{state.switching ? '正在切换并校验会话…' : '已预约切换，可在主界面取消。'}</div>}
        <div className="notice"><CircleAlert size={18}/><span>有头 / 无头不是简单隐藏窗口。若登录无法迁移，会暂停并请你粘贴新链接，已下载文件和任务进度保留。</span></div>
        <div className="setting-row"><div><b>浏览器窗口</b><small>最小化不会切换为无头，也不会重启会话。</small></div></div>
        <div className="button-row"><button className="btn" disabled={locked || !state.session_ready || state.headless || state.switching} onClick={() => void command('browser_window', { action: 'minimize' })}><Minus size={16}/>最小化浏览器</button>
          <button className="btn" disabled={locked || !state.session_ready || state.headless || state.switching} onClick={() => void command('browser_window', { action: 'restore' })}><Monitor size={16}/>还原浏览器</button></div>
        <label className="browser-path">浏览器可执行文件（可选）<div className="input-with-button"><input value={browserPath} onChange={e => setBrowserPath(e.target.value)} placeholder="默认使用随包浏览器 / 系统 Chrome"/>
          <button className="btn" onClick={() => void attempt('browser', async () => { const p = await api?.browser(); if (p) setBrowserPath(p); })}>选择</button></div></label>
        <p className="hint">自定义路径在下次连接时生效。清空后恢复自动选择。</p>
        <div className="setting-row"><div><b>悬浮窗置顶</b><small>可拖动、靠边吸附，记住上次位置。</small></div><button role="switch" aria-checked={top} aria-label="悬浮窗置顶" className={'switch ' + (top ? 'on' : '')} onClick={() => showWindow('top', !top)}><i/></button></div>
        <div className="setting-row"><div><b>本机任务库</b><small className="break">{state.state_dir || '连接后台后显示'}</small></div><button className="icon-button" aria-label="打开任务库目录" disabled={!state.state_dir} onClick={() => void attempt('open', () => api!.open(state.state_dir))}><FolderOpen size={19}/></button></div>
      </div><div className="modal-footer"><button className="btn primary" onClick={() => setModal(null)}>完成</button></div></>}
      {modal === 'import' && <><div className="modal-body import-body"><div className="import-config">
        <label>1. 选择参数表<button aria-label="选择 Excel 参数表" className="upload-zone" disabled={!!busy || locked} onClick={() => void chooseFiles()}><FileSpreadsheet size={27}/><b>选择 Excel 参数表</b><span>可多选，也可直接拖入窗口</span></button></label>
        <div className="file-list">{paths.map(p => <div key={p}><FileSpreadsheet size={15}/><span title={p}>{shortName(p)}</span><button className="icon-button" disabled={!!busy} aria-label={'移除 ' + shortName(p)} onClick={() => { void discardPreview(); setPaths(prev => prev.filter(x => x !== p)); }}><X size={14}/></button></div>)}</div>
        <label>2. 下载目录<button aria-label="选择下载目录" className="directory-choice" disabled={!!busy} onClick={() => void attempt('directory', async () => { const p = await api?.directory(); if (p) setRoot(p); })}><FolderOpen size={17}/><span>{root || '选择本机文件夹'}</span><ChevronRight size={15}/></button></label>
        <p className="hint">每份参数表会创建独立子目录，不覆盖已有文件。</p>
        <label>文件命名<select value={options.naming_mode} disabled={!!busy} onChange={e => void changeOptions({ ...options, naming_mode: e.target.value })}><option value="param">优先使用参数表“文件名称”</option><option value="auto">自动命名（日期 / 国库 / 报表）</option></select></label>
        <div className="two-col"><label>后备起始日期<input type="date" value={options.start_date} disabled={!!busy} onChange={e => void changeOptions({ ...options, start_date: e.target.value })}/></label>
          <label>后备终止日期<input type="date" value={options.end_date} disabled={!!busy} onChange={e => void changeOptions({ ...options, end_date: e.target.value })}/></label></div>
        <p className="hint">仅在参数表日期留空时使用，通常无需填写。</p>
        <label className="checkbox-label"><input type="checkbox" disabled={!!busy} checked={options.postprocess} onChange={e => void changeOptions({ ...options, postprocess: e.target.checked })}/>启用既有 Excel 数据后处理</label>
        {options.postprocess && <p className="hint amber">将沿用旧版清洗 / 汇总规则，可能改变报表内容；保留原始报表请勿勾选。</p>}
      </div><div className="import-preview"><div className="panel-title"><h3>3. 导入前检查</h3><span>仅校验参数，不发起查询</span></div>
        {!previews.length ? <div className="preview-empty"><ListFilter size={32}/><h3>先检查，再加入队列</h3><p>识别报表类型、任务数量和日期范围，<br/>检查重复文件名及缺失参数。</p><button className="btn" disabled={!paths.length || !!busy || locked} onClick={() => void attempt('preview', async () => setPreviews(await api!.call<Preview[]>('preview', { paths, options })))}>
          {busy === 'preview' ? <LoaderCircle className="spin" size={16}/> : <CheckCircle2 size={16}/>}检查参数</button></div> :
          <><div className="preview-summary"><b>{previews.reduce((n, p) => n + (p.count || 0), 0)}</b><span>条待导入任务 / {previews.filter(p => p.id).length} 份参数表</span></div>
            {previews.map((p, i) => <article className={'preview-file ' + (p.error ? 'invalid' : '')} key={p.id || i}><b><FileSpreadsheet size={17}/>{p.name}</b>
              {p.error ? <p className="red">{p.error}</p> : <><p>{Object.entries(p.kinds || {}).map(([k, n]) => k + ' ' + n + ' 条').join(' · ')}</p><small>{date(p.start)} — {date(p.end)}</small>
                {p.duplicate && <div className="amber">检测到相同参数内容，需要确认重复导入。</div>}
                <details><summary>查看参数样例</summary>{p.sample?.map((sample, n) => <div key={n} className="sample"><b>{sample.output_name}</b><dl>{Object.entries(sample.params).filter(([k]) => ['pTreCode', 'pStartDate', 'pEndDate', 'pSbtCode', 'pBookSbt', 'pGovernFlag', 'pShowScope', 'pBdgLevel'].includes(k)).map(([k, v]) => <React.Fragment key={k}><dt>{({ pTreCode: '国库', pStartDate: '起始日期', pEndDate: '终止日期', pSbtCode: '预算科目', pBookSbt: '会计科目', pGovernFlag: '辖属标志', pShowScope: '展示范围', pBdgLevel: '预算级次' } as Record<string, string>)[k]}</dt><dd>{v}</dd></React.Fragment>)}</dl></div>)}</details></>}
            </article>)}
            {previews.some(p => p.duplicate) && <label className="checkbox-label"><input type="checkbox" checked={duplicate} onChange={e => setDuplicate(e.target.checked)}/>确认重复导入（会生成新的独立批次）</label>}
            <p className="hint">导入使用本次检查的参数快照；之后修改源文件不会改变已排队任务。预览有效期 15 分钟。</p>
          </>}
      </div></div><div className="modal-footer"><span>{running ? '新任务会追加到当前队尾' : '导入后由你点击开始，不会自动运行'}</span>
        <button className="btn" disabled={!!busy} onClick={() => setModal(null)}>稍后继续</button><button className="btn primary" disabled={!!busy || locked || !root || !previews.some(p => p.id) || (previews.some(p => p.duplicate) && !duplicate)}
          onClick={() => void importPlans()}>{busy === 'import' ? <LoaderCircle className="spin" size={16}/> : <Plus size={16}/>}加入队列</button></div></>}
    </section></div>}
    {detail && <div className="modal-shade"><section className="modal detail-modal" role="dialog" aria-modal="true" aria-labelledby="detail-title"><div className="modal-header"><div><div className="eyebrow">TASK DETAILS</div><h2 id="detail-title">任务详情与执行记录</h2></div><button className="icon-button" aria-label="关闭详情" onClick={() => setDetail(null)}><X size={20}/></button></div>
      <div className="modal-body"><h3 className="break">{detail.task.output_name}</h3><Badge status={detail.task.status}/><p className="hint">{detail.task.source_name} · 第 {detail.task.row_number} 行</p>
        {detail.task.error && <div className="notice warning break">{detail.task.error}</div>}
        <details><summary>查看全部参数（导入快照）</summary><dl className="params">{Object.entries(detail.task.params).map(([k, v]) => <React.Fragment key={k}><dt>{k}</dt><dd>{String(v)}</dd></React.Fragment>)}</dl></details>
        <h3>执行历史</h3>{detail.history.length ? detail.history.map((item: any, i: number) => <div className="history" key={i}><span className="history-index">{i + 1}</span><div><b>{statusName[item.status] || item.status} · {item.stage || '开始执行'}</b><small>{item.started || ''}</small>{item.error && <p className="break">{item.error}</p>}</div></div>) : <p className="hint">尚未执行</p>}
      </div><div className="modal-footer"><button className="btn" onClick={() => void attempt('open', () => api!.open(detail.task.output_dir))}><FolderOpen size={16}/>打开输出目录</button>
        {retryable(detail.task) && <button className="btn primary" disabled={locked || !!busy} onClick={() => { void command('retry', { ids: [detail.task.id] }); setDetail(null); }}><RefreshCw size={16}/>重试此任务</button>}</div>
    </section></div>}
    {toast && <div className="toast" role="status"><CircleAlert size={18}/><span>{toast}</span><button className="icon-button" aria-label="关闭消息" onClick={() => setToast('')}><X size={16}/></button></div>}
    {dragging && <div className="drop-overlay"><FileSpreadsheet size={48}/><h2>松开，添加参数表</h2><p>多份 Excel 将一起进入导入检查</p></div>}
  </div>;
}
createRoot(document.getElementById('root')!).render(<App/>);
