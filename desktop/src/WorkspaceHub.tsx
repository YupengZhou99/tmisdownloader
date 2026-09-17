import React, { useEffect, useState } from 'react';
import { ArrowDownToLine, ArrowUpRight, CheckCircle2, ChevronRight, CircleAlert, Files, FolderOpen,
  LayoutDashboard, Link, LoaderCircle, LogOut, Maximize2, Monitor, Pause, Pin, Play, Plus, ShieldCheck, X } from 'lucide-react';
import type { WorkspaceInfo, WorkspaceSnapshot } from './types';
const compact = new URLSearchParams(location.search).get('view') === 'compact';
const modeNames: Record<string, string> = { running: '正在下载', pausing: '等待当前条结束', paused: '已暂停', idle: '服务待命',
  waiting_login: '等待登录', logging_in: '正在登录', switching: '切换浏览器中' };
const count = (w: WorkspaceInfo, key: string) => w.state.counts[key] || 0;
const failed = (w: WorkspaceInfo) => count(w, 'failed') + count(w, 'timed_out') + count(w, 'interrupted');
const total = (w: WorkspaceInfo) => w.state.tasks.length - count(w, 'cancelled');
const done = (w: WorkspaceInfo) => count(w, 'succeeded') + count(w, 'failed') + count(w, 'timed_out');
const errorText = (error: unknown) => error instanceof Error ? error.message.replace(/^Error invoking remote method '[^']+': Error: /, '') : String(error);
export default function WorkspaceHub({ children }: { children: (w: WorkspaceInfo, overview: () => void, manage: () => void, switchTo: (id: string) => void, workspaces: WorkspaceInfo[]) => React.ReactNode }) {
  const api = window.tmis;
  const [hub, setHub] = useState<WorkspaceSnapshot>({ version: '6.1.0', limit: 4, workspaces: [], closing: false });
  const [selected, select] = useState<string | null>(null), [wizard, setWizard] = useState(false);
  const [urls, setUrls] = useState<Record<string, string>>({}), [bulk, setBulk] = useState('');
  const [names, setNames] = useState<Record<string, string>>({}), [busy, setBusy] = useState('');
  const [toast, setToast] = useState(''), [top, setTop] = useState(true), [failedOnly, setFailedOnly] = useState(false);
  const active = hub.workspaces.filter(w => !w.archived), archived = hub.workspaces.filter(w => w.archived);
  const pending = active.filter(w => !w.confirmed), current = active.find(w => w.id === selected);
  const allTotal = active.reduce((n, w) => n + total(w), 0), allDone = active.reduce((n, w) => n + done(w), 0);
  const allFailed = active.reduce((n, w) => n + failed(w), 0), allSuccess = active.reduce((n, w) => n + count(w, 'succeeded'), 0);
  const running = active.filter(w => ['running', 'pausing'].includes(w.state.mode));
  async function refresh() { if (api) setHub(await api.workspaces<WorkspaceSnapshot>('snapshot')); }
  useEffect(() => {
    if (!api) return;
    void api.workspaces<WorkspaceSnapshot>('snapshot').then(s => { setHub(s); setTop(s.top !== false); }).catch(e => setToast(errorText(e)));
    return api.subscribe(event => {
      if (event.event === 'workspaces') setHub(event.data);
      if (event.event === 'navigate') select(event.workspaceId);
      if (event.event === 'window') setTop(event.top);
      if (event.event === 'closing') setHub(s => ({ ...s, closing: true }));
    });
  }, []);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(''), 12000); return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'm' && !current) {
        e.preventDefault(); void api?.window(compact ? 'expand' : 'compact');
      }
    };
    window.addEventListener('keydown', key); return () => window.removeEventListener('keydown', key);
  }, [current]);
  async function action<T = unknown>(name: string, data = {}): Promise<T | undefined> {
    if (!api) return;
    setBusy(name);
    try { const result = await api.workspaces<T>(name, data); await refresh(); return result; }
    catch (error) { setToast(errorText(error)); }
    finally { setBusy(''); }
  }
  async function runAll(pause = false) {
    const result = await action<{ name: string; ok: boolean; error?: string }[]>(pause ? 'pause_all' : 'start_all');
    if (result) { const skipped = result.filter(r => !r.ok); setToast(skipped.length ? skipped.map(r => r.name + '：' + r.error).join('；') : pause ? '各队列将在当前条结束后暂停' : '已向所有就绪队列发送开始指令'); }
  }
  async function add() {
    const id = await action<string>('create', { name: '工作区 ' + (active.length + 1) });
    if (id) setWizard(true);
  }
  async function distribute() {
    const lines = bulk.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    if (!lines.length || lines.some(s => !/^https?:\/\/\S+$/i.test(s))) { setToast('请每行粘贴一个完整 HTTP(S) 登录链接'); return; }
    const targets = pending.filter(w => !w.state.session_ready && !w.busy).map(w => w.id);
    if (lines.length > targets.length + hub.limit - active.length) { setToast('可用工作区不足，最多同时登录 4 个会话'); return; }
    setBusy('distribute');
    try {
      while (targets.length < lines.length) targets.push(await api!.workspaces<string>('create', { name: '工作区 ' + (active.length + targets.length - pending.length + 1) }));
      setUrls(prev => ({ ...prev, ...Object.fromEntries(lines.map((url, i) => [targets[i], url])) }));
      setBulk(''); await refresh();
    } catch (error) { setToast(errorText(error)); } finally { setBusy(''); }
  }
  async function login(ids: string[]) {
    const entries = ids.filter(id => urls[id]?.trim()).map(id => ({ id, url: urls[id].trim() }));
    if (!entries.length) { setToast('请先填写登录链接'); return; }
    setUrls(prev => { const next = { ...prev }; entries.forEach(e => { delete next[e.id]; }); return next; });
    const result = await action<{ id: string; ok: boolean; error?: string }[]>('login_all', { entries });
    if (result?.some(r => !r.ok)) setToast(result.filter(r => !r.ok).map(r => r.error).join('；'));
  }
  const closeWizard = () => { setWizard(false); setUrls({}); setBulk(''); };
  const readyToConfirm = pending.length > 0 && pending.every(w => w.online && w.state.session_ready && !w.busy);
  const status = (w: WorkspaceInfo) => w.busy === 'login' ? '登录中' : w.state.session_ready ? (w.confirmed ? modeNames[w.state.mode] || '已就绪' : '已登录 · 待确认') : w.error ? '需要处理' : '等待登录';
  const show = (id: string) => { select(id); setFailedOnly(false); };
  const windowAction = (name: string, value?: boolean | string) => void api?.window(name, value).catch(e => setToast(errorText(e)));
  if (compact) return <div className="compact multi-compact">
    <div className="compact-title"><span className="logo small"><ArrowDownToLine size={17}/></span><b>TMIS 并行下载</b><span className="grow"/>
      <button className={'icon-button nodrag ' + (top ? 'blue' : '')} aria-label="窗口置顶" onClick={() => windowAction('top', !top)}><Pin size={16}/></button>
      <button className="icon-button nodrag" aria-label="返回工作台" onClick={() => windowAction('expand')}><Maximize2 size={16}/></button></div>
    <div className="compact-status"><span className={'live-dot ' + (running.length ? 'active' : '')}/>{hub.closing ? '正在退出' : running.length ? running.length + ' 路正在运行' : '所有队列待命'}<span>{allTotal ? Math.round(allDone / allTotal * 100) : 0}%</span></div>
    <div className="progress"><i style={{ width: (allTotal ? allDone / allTotal * 100 : 0) + '%' }}/></div>
    <div className="compact-workspaces">{active.map((w, i) => <button key={w.id} onClick={() => windowAction('workspace', w.id)} aria-label={'打开工作区 ' + w.name}>
      <span className={'workspace-dot color-' + i}/><b>{w.name}</b><span>{count(w, 'succeeded')} / {total(w)}</span><small className={failed(w) ? 'amber' : ''}>{failed(w) ? failed(w) + ' 待处理' : status(w)}</small></button>)}</div>
    <div className="compact-metrics"><span>成功 {allSuccess} / {allTotal}</span><span className={allFailed ? 'amber' : ''}>待处理 {allFailed}</span></div>
    <div className="compact-actions"><button className="btn soft" disabled={!!busy || hub.closing} onClick={() => void runAll(!!running.length)}>{running.length ? <Pause size={15}/> : <Play size={15}/>} {running.length ? '全部暂停' : '全部开始'}</button>
      <button className="btn" onClick={() => windowAction('expand')}>返回工作台 <ArrowUpRight size={15}/></button></div>
    {toast && <button className="compact-toast" onClick={() => { setToast(''); windowAction('expand'); }}>{toast}</button>}
  </div>;
  return <>
    {current ? children(current, () => select(null), () => setWizard(true), show, active) : <div className="shell hub-shell">
      <aside className="sidebar"><div className="brand"><span className="logo"><ArrowDownToLine size={24}/></span><div><b>TMIS</b><span>数据工作台</span></div><span className="version">6.1</span></div>
        <div className="sidebar-caption">工作空间</div><button className={'nav ' + (!failedOnly ? 'active' : '')} onClick={() => setFailedOnly(false)}><LayoutDashboard size={18}/>工作区总览</button>
        <button className={'nav ' + (failedOnly ? 'active' : '')} onClick={() => setFailedOnly(true)}><CircleAlert size={18}/>全部待处理任务{allFailed > 0 && <em>{allFailed}</em>}</button>
        <div className="sidebar-caption">独立队列 · {active.length} / 4</div><div className="workspace-nav">{active.map((w, i) => <button className="nav" key={w.id} onClick={() => show(w.id)}><span className={'workspace-dot color-' + i}/><span>{w.name}<small>{status(w)}</small></span></button>)}</div>
        <div className="sidebar-bottom"><div><ShieldCheck size={14}/> 链接仅保留在内存</div><button className="nav" onClick={() => windowAction('exit')}><LogOut size={17}/>退出所有工作区</button></div>
      </aside>
      <section className="workspace"><header className="topbar"><span>工作空间 <ChevronRight size={14}/> 并行总览</span><button className="btn" onClick={() => windowAction('compact')}><Monitor size={16}/>悬浮窗</button></header><main>
        <div className="page-heading"><div><div className="eyebrow">PARALLEL WORKSPACES</div><h1>{failedOnly ? '全部待处理任务' : '每一路，各司其职。'}</h1><p>先确认登录，再分别配置参数。浏览器、任务和保存位置始终一一对应。</p></div><button className="btn primary" disabled={!api || !!busy || active.length >= 4 || hub.closing} onClick={() => void add()}><Plus size={17}/>新增工作区</button></div>
        {!api && <div className="notice warning">当前为外观预览。登录、文件选择和并行下载仅在桌面应用中可用。</div>}
        {hub.closing && <div className="notice warning">正在保存所有队列并关闭浏览器…</div>}
        {pending.length > 0 && <div className="onboarding-card"><span className="step-number">01</span><div><b>先把本轮 {pending.length} 个登录会话准备好</b><p>{pending.filter(w => w.state.session_ready).length} / {pending.length} 已登录。全部成功后，确认进入参数配置。</p></div><button className="btn primary" disabled={hub.closing} onClick={() => setWizard(true)}><Link size={16}/>批量登录与确认</button></div>}
        <div className="hub-summary"><span><b>{active.length}</b> 个独立工作区</span><span><b>{running.length}</b> 路运行中</span><span><b>{allSuccess}</b> / {allTotal} 成功</span><span className={allFailed ? 'amber' : ''}><b>{allFailed}</b> 待处理</span><span className="grow"/>
          <button className="btn" disabled={!api || !!busy || hub.closing} onClick={() => void runAll(true)}><Pause size={15}/>全部暂停</button><button className="btn primary" disabled={!api || !!busy || hub.closing || !active.some(w => w.confirmed && w.state.session_ready)} onClick={() => void runAll()}><Play size={15}/>全部开始</button></div>
        {failedOnly ? <section className="panel all-failures">{active.flatMap(w => w.state.tasks.filter(t => ['failed', 'timed_out', 'interrupted'].includes(t.status)).map(t => <article key={w.id + t.id}>
          <CircleAlert size={18}/><div><b>{t.output_name}</b><small>{w.name} · {t.source_name}</small><p>{t.error}</p></div><button className="btn" onClick={() => show(w.id)}>进入所属队列 <ChevronRight size={15}/></button></article>))}
          {!allFailed && <div className="empty-state"><CheckCircle2 size={32}/><h3>所有工作区都没有待处理任务</h3><p>失败记录始终保留所属工作区，不自动转移任务。</p></div>}</section> : <div className="workspace-grid">{active.map((w, i) => <article className={'workspace-card color-' + i} key={w.id}>
          <header><span className="workspace-avatar">{String(i + 1).padStart(2, '0')}</span><div><h2>{w.name}</h2><span className={w.state.session_ready ? 'green-text' : 'muted'}>{status(w)}</span></div><span className="grow"/><button className="icon-button" aria-label={'归档 ' + w.name} title="归档并保留队列" disabled={!!w.busy || !!w.state.current || hub.closing} onClick={() => { if (confirm('归档“' + w.name + '”并关闭连接？任务和文件会保留，可在下方恢复。')) void action('archive', { id: w.id }); }}><X size={17}/></button></header>
          <div className="workspace-progress"><b>{count(w, 'succeeded')}<small> / {total(w)} 成功</small></b><span>{failed(w)} 待处理</span></div><div className="progress"><i style={{ width: (total(w) ? done(w) / total(w) * 100 : 0) + '%' }}/></div>
          <p className="workspace-current">{w.state.tasks.find(t => t.id === w.state.current)?.output_name || (w.confirmed ? '在此队列中添加参数表，或继续已有任务' : '等待本轮登录确认后配置任务')}</p>
          <div className="workspace-path"><FolderOpen size={15}/><span title={w.root}>{w.root || '尚未选择保存根目录'}</span></div>
          {w.error && <div className="workspace-error">{w.error}</div>}
          <footer><button className="btn" onClick={() => show(w.id)} aria-label={'进入队列 ' + w.name}>进入队列 <ChevronRight size={15}/></button>
            {!w.confirmed && <button className="text-button" onClick={() => setWizard(true)}>登录与确认</button>}
            {w.confirmed && <button className="text-button" disabled={!w.state.session_ready || w.state.headless} onClick={() => void api?.call('browser_window', { action: 'restore' }, w.id).catch(e => setToast(errorText(e)))}>查看浏览器</button>}</footer>
        </article>)}</div>}
        {archived.length > 0 && <section className="archived-list"><h3>已归档 · 队列和文件仍保留</h3>{archived.map(w => <div key={w.id}><Files size={16}/><b>{w.name}</b><span className="grow"/><button className="btn" disabled={active.length >= 4 || !!busy} onClick={async () => { await action('restore', { id: w.id }); setWizard(true); }}>恢复工作区</button></div>)}</section>}
        <footer className="page-footer"><span><ShieldCheck size={14}/> 四路独立运行 · 文件保存在本机</span><span>TMIS Workbench / {hub.version}</span></footer>
      </main></section>
    </div>}
    {wizard && <div className="modal-shade"><section className="modal login-wizard" role="dialog" aria-modal="true" aria-labelledby="login-wizard-title">
      <div className="modal-header"><div><div className="eyebrow">CONNECT → CONFIRM → CONFIGURE</div><h2 id="login-wizard-title">先确认全部登录，再配置各自队列</h2></div><button className="icon-button" aria-label="关闭登录向导" onClick={closeWizard}><X size={20}/></button></div>
      <div className="modal-body"><div className="notice"><ShieldCheck size={18}/><span>每个链接对应独立浏览器。提交后清空输入；不保存登录 URL 或 Token。已运行的工作区不受新增登录影响。</span></div>
        <div className="bulk-login"><label>批量粘贴登录链接（每行一个）<textarea rows={2} autoComplete="off" spellCheck={false} value={bulk} onChange={e => setBulk(e.target.value)} placeholder="每行一个完整登录 URL，分配后可分别核对"/></label><button className="btn" disabled={!bulk.trim() || !!busy} onClick={() => void distribute()}>分配到工作区</button></div>
        <div className="login-rows">{pending.map((w, i) => <article className="login-row" key={w.id} data-workspace-id={w.id}>
          <div className="login-row-heading"><span className="step-number">{i + 1}</span><input aria-label={'工作区名称 ' + w.id} value={names[w.id] ?? w.name} maxLength={40} onChange={e => setNames(n => ({ ...n, [w.id]: e.target.value }))} onBlur={() => { if (names[w.id]?.trim() && names[w.id] !== w.name) void action('update', { id: w.id, settings: { name: names[w.id] } }); }}/><span className={w.state.session_ready ? 'green-text' : 'muted'}>{status(w)}</span></div>
          <div className="input-with-button"><input type="text" aria-label={'登录链接 ' + w.name} value={urls[w.id] || ''} disabled={!!w.busy} autoComplete="off" spellCheck={false} placeholder={w.state.session_ready ? '已登录，无需重复粘贴' : '粘贴此浏览器的完整登录链接'} onChange={e => setUrls(prev => ({ ...prev, [w.id]: e.target.value }))}/>
            <button className="btn" disabled={!urls[w.id]?.trim() || !!w.busy} onClick={() => void login([w.id])}>{w.busy ? <LoaderCircle className="spin" size={15}/> : <Link size={15}/>}单独登录</button></div>
          {w.error && <p className="red break">{w.error}</p>}
          <div className="login-row-actions"><label className="browser-path">浏览器路径（可选）<input aria-label={'浏览器路径 ' + w.name} defaultValue={w.browserPath} placeholder="默认随包浏览器 / 系统 Chrome" onBlur={e => { if (e.target.value !== w.browserPath) void action('update', { id: w.id, settings: { browserPath: e.target.value } }); }}/></label>
            <button className="text-button" disabled={!w.state.session_ready || w.state.headless} onClick={() => void api?.call('browser_window', { action: 'restore' }, w.id).catch(e => setToast(errorText(e)))}>查看浏览器</button>
            <button className="text-button muted" disabled={!!w.busy} onClick={() => { if (confirm('将“' + w.name + '”移出本轮并归档？已有队列保留。')) void action('archive', { id: w.id }); }}>移出本轮</button></div>
        </article>)}</div>
        {!pending.length && <div className="notice success"><CheckCircle2 size={18}/>当前活动工作区均已完成登录确认，可分别配置任务。</div>}
        <button className="text-button" disabled={active.length >= 4 || !!busy} onClick={() => void add()}><Plus size={15}/>再添加一个会话（{active.length}/4）</button>
      </div><div className="modal-footer"><span>{pending.filter(w => w.state.session_ready).length} / {pending.length} 本轮会话已登录</span><button className="btn" disabled={!!busy || !pending.some(w => urls[w.id]?.trim())} onClick={() => void login(pending.map(w => w.id))}>全部登录</button>
        <button className="btn primary" disabled={!readyToConfirm || !!busy || hub.closing} onClick={async () => {
          if (!api) return;
          setBusy('confirm');
          try { await api.workspaces('confirm', { ids: pending.map(w => w.id) }); await refresh(); closeWizard(); select(null); setToast('登录已确认。现在可以分别配置参数表和保存目录。'); }
          catch (error) { setToast(errorText(error)); } finally { setBusy(''); }
        }}><CheckCircle2 size={16}/>确认并配置任务</button></div>
    </section></div>}
    {toast && <div className="toast" role="status"><CircleAlert size={18}/><span>{toast}</span><button className="icon-button" aria-label="关闭全局消息" onClick={() => setToast('')}><X size={16}/></button></div>}
  </>;
}
