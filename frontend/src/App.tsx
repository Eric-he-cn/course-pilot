import { ChangeEvent, FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, api } from './api'
import type { ArchiveSummary, Citation, Course, Job, Material, Message, Plan, ScopeMode, SearchResult, SessionSummary } from './types'

type View = 'chat' | 'library' | 'plan' | 'archive' | 'settings'
type Workspace = { scope: ScopeMode; courseId?: string }
type TurnResolution = { sessionId: string; status: string; courseId: string | null; courseName: string | null }

const viewNames: Record<View, string> = { chat: '对话', library: '知识仓库', plan: '学习计划', archive: '学习档案', settings: '管理与设置' }
const nav: { id: View; icon: string }[] = [
  { id: 'chat', icon: '◌' }, { id: 'library', icon: '▤' }, { id: 'plan', icon: '□' }, { id: 'archive', icon: '◫' },
]
const MAX_MATERIAL_BYTES = 100 * 1024 * 1024

function errorText(error: unknown) { return error instanceof Error ? error.message : '发生未知错误，请重试。' }
function timeLabel(value?: string) { return value ? new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', month: 'numeric', day: 'numeric' }).format(new Date(value)) : '刚刚' }

export default function App() {
  const [courses, setCourses] = useState<Course[]>([])
  const [workspace, setWorkspace] = useState<Workspace>({ scope: 'general' })
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [activeSession, setActiveSession] = useState<SessionSummary | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [view, setView] = useState<View>('chat')
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => localStorage.getItem('cp-sidebar-collapsed') === 'true')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [apiOnline, setApiOnline] = useState<boolean | null>(null)
  const [citation, setCitation] = useState<Citation | null>(null)
  const [turnResolution, setTurnResolution] = useState<TurnResolution | null>(null)

  const course = useMemo(() => courses.find(item => item.id === workspace.courseId) ?? null, [courses, workspace.courseId])
  const heading = activeSession?.title && view === 'chat' ? activeSession.title : viewNames[view]

  useEffect(() => { localStorage.setItem('cp-sidebar-collapsed', String(sidebarCollapsed)) }, [sidebarCollapsed])
  useEffect(() => {
    api.health().then(() => setApiOnline(true)).catch(() => setApiOnline(false))
    api.courses().then(setCourses).catch(error => setNotice(errorText(error)))
  }, [])
  useEffect(() => { void loadSessions() }, [workspace.scope, workspace.courseId])
  useEffect(() => { setTurnResolution(null); if (activeSession) void loadMessages(activeSession.id) }, [activeSession?.id])

  async function loadSessions() {
    try {
      const result = await api.sessions(workspace.scope, workspace.courseId)
      setSessions(result)
      setActiveSession(current => current && result.some(item => item.id === current.id) ? current : result[0] ?? null)
    } catch (error) { setSessions([]); setActiveSession(null); setNotice(errorText(error)) }
  }
  async function loadMessages(id: string) {
    try { setMessages(await api.messages(id)) } catch (error) { setMessages([]); setNotice(errorText(error)) }
  }
  function switchWorkspace(next: Workspace) {
    setWorkspace(next); setView('chat'); setSidebarOpen(false); setCitation(null); setTurnResolution(null)
  }
  async function newSession() {
    setBusy(true)
    try {
      const session = await api.createSession(workspace.scope, workspace.courseId)
      setSessions(current => [session, ...current]); setActiveSession(session); setView('chat'); setMessages([])
    } catch (error) { setNotice(errorText(error)) } finally { setBusy(false) }
  }
  async function createCourse() {
    const name = window.prompt('课程名称')?.trim(); if (!name) return
    setBusy(true)
    try { const created = await api.createCourse(name); setCourses(current => [...current, created]); switchWorkspace({ scope: 'course', courseId: created.id }) }
    catch (error) { setNotice(errorText(error)) } finally { setBusy(false) }
  }

  const workspaceName = workspace.scope === 'general' ? '通用模式' : course?.name ?? '课程工作区'
  return <div className={`app ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
    {sidebarOpen && <button className="sidebar-backdrop" aria-label="关闭导航" onClick={() => setSidebarOpen(false)} />}
    <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`} aria-label="课程与会话">
      <div className="brand"><div className="brandmark">CP</div><div className="brand-copy"><strong>CoursePilot</strong><span>Personal tutor</span></div></div>
      <button className={`workspace-card ${workspace.scope === 'general' ? 'selected' : ''}`} onClick={() => switchWorkspace({ scope: 'general' })}>
        <span className="general-icon">✦</span><span className="workspace-copy"><b>通用模式</b><small>每轮按问题解析课程</small></span>
      </button>
      <div className="course-switcher">
        <div className="side-label">课程工作区</div>
        {courses.map(item => <button className={`course-choice ${item.id === workspace.courseId ? 'selected' : ''}`} key={item.id} onClick={() => switchWorkspace({ scope: 'course', courseId: item.id })}>
          <i style={{ backgroundColor: item.color }} /><span>{item.name}</span>{item.wiki_enabled && <em>Wiki</em>}
        </button>)}
        <button className="text-button add-course" onClick={createCourse} disabled={busy}>＋ 新建课程</button>
      </div>
      <nav className="main-nav" aria-label="学习导航">
        {nav.map(item => <button className={view === item.id ? 'active' : ''} key={item.id} onClick={() => { setView(item.id); setSidebarOpen(false) }}><span aria-hidden>{item.icon}</span><b>{viewNames[item.id]}</b></button>)}
      </nav>
      <div className="sessions-head"><span>会话</span><button aria-label="新建会话" onClick={newSession} disabled={busy}>＋</button></div>
      <div className="session-list">
        {sessions.length ? sessions.map(session => <button className={`session ${session.id === activeSession?.id ? 'active' : ''}`} key={session.id} onClick={() => { setActiveSession(session); setView('chat'); setSidebarOpen(false) }}>
          <span className="session-marker" title={session.scope_mode === 'general' ? '通用会话' : '课程会话'}>{session.scope_mode === 'general' ? '✦' : '●'}</span>
          <i style={{ backgroundColor: session.course_color ?? '#99A19D' }} /><span className="session-text"><b>{session.title || '未命名会话'}</b><small>{timeLabel(session.updated_at)}</small></span>
        </button>) : <p className="mini-empty">此工作区还没有会话。</p>}
      </div>
      <button className="new-session" onClick={newSession} disabled={busy}>＋ 新建{workspace.scope === 'general' ? '通用' : '课程'}会话</button>
      <div className="sidebar-foot"><button onClick={() => { setView('settings'); setSidebarOpen(false) }}>⚙ <span>管理与设置</span></button></div>
    </aside>
    <main className="main">
      <header className="topbar">
        <button className="icon-button mobile-only" aria-label="打开导航" onClick={() => setSidebarOpen(true)}>☰</button>
        <button className="icon-button collapse-only" aria-label="折叠侧栏" onClick={() => setSidebarCollapsed(value => !value)}>☷</button>
        <div className="title-area"><b>{heading}</b><span className="crumb"><i style={{ backgroundColor: course?.color ?? '#7B8881' }} /> {workspaceName}</span></div>
        <div className="connection"><i className={apiOnline ? 'online' : 'offline'} /> <span>{apiOnline ? '服务已连接' : '服务未连接'}</span></div>
        {view === 'chat' && <button className="ghost-button" onClick={() => setCitation([...messages].reverse().find(item => item.citations?.length)?.citations?.[0] ?? null)}>本轮引用</button>}
      </header>
      {notice && <div className="notice" role="alert"><span>{notice}</span><button aria-label="关闭错误提示" onClick={() => setNotice('')}>×</button></div>}
      {view === 'chat' && <ChatView session={activeSession} messages={messages} workspaceName={workspaceName} turnResolution={turnResolution} onCitation={setCitation} onSend={async content => {
        let targetSession = activeSession
        if (!targetSession) {
          setBusy(true)
          try {
            targetSession = await api.createSession(workspace.scope, workspace.courseId)
            setSessions(current => [targetSession!, ...current]); setActiveSession(targetSession); setView('chat'); setMessages([])
          } catch (error) { setNotice(errorText(error)); setBusy(false); return }
        }
        const optimistic: Message = { id: `pending-user-${Date.now()}`, role: 'user', content }
        const pendingId = `pending-assistant-${Date.now()}`
        setMessages(current => [...current, optimistic, { id: pendingId, role: 'assistant', content: '' }]); setBusy(true)
        try { await api.turn(targetSession.id, content, payload => {
          const resolved = payload.type === 'course_resolution' || payload.event === 'course_resolution'
          if (resolved) {
            const isResolved = payload.status === 'resolved'
            const resolvedId = isResolved ? payload.resolved_course_id ?? payload.course_id ?? null : null
            setTurnResolution({ sessionId: targetSession.id, status: payload.status ?? 'unresolved', courseId: resolvedId, courseName: isResolved ? payload.course_name ?? null : null })
            setActiveSession(current => current ? { ...current, resolved_course_id: resolvedId, course_name: isResolved ? payload.course_name ?? current.course_name : null, course_color: isResolved ? payload.course_color ?? current.course_color : null } : current)
            setSessions(current => current.map(item => item.id === targetSession.id ? { ...item, resolved_course_id: resolvedId, course_name: isResolved ? payload.course_name ?? item.course_name : null, course_color: isResolved ? payload.course_color ?? item.course_color : null } : item))
          }
          const delta = payload.delta ?? payload.content ?? payload.text ?? ''
          if (delta) setMessages(current => current.map(item => item.id === pendingId ? { ...item, content: item.content + delta } : item))
        }); await loadMessages(targetSession.id); await loadSessions() }
        catch (error) {
          setNotice(errorText(error))
          // 优先回读服务端真值（部分回答已带 interrupted 状态持久化）；服务不可达时保留本地标记。
          try { await loadMessages(targetSession.id); await loadSessions() }
          catch { setMessages(current => current.map(item => item.id === pendingId ? { ...item, content: item.content || '本次回答未能完成。', artifact: { kind: 'interrupted' } } : item)) }
        }
        finally { setBusy(false) }
      }} busy={busy} />}
      {view === 'library' && <LibraryView course={course} onCourseChange={updated => setCourses(current => current.map(item => item.id === updated.id ? updated : item))} onError={setNotice} />}
      {view === 'plan' && <PlanView course={course} onError={setNotice} />}
      {view === 'archive' && <ArchiveView course={course} onError={setNotice} />}
      {view === 'settings' && <SettingsView courses={courses} onError={setNotice} />}
    </main>
    {citation && <CitationDrawer citation={citation} onClose={() => setCitation(null)} />}
  </div>
}

function ChatView({ session, messages, workspaceName, turnResolution, onCitation, onSend, busy }: { session: SessionSummary | null; messages: Message[]; workspaceName: string; turnResolution: TurnResolution | null; onCitation: (citation: Citation) => void; onSend: (content: string) => Promise<void>; busy: boolean }) {
  const [draft, setDraft] = useState(''); const composer = useRef<HTMLTextAreaElement>(null)
  async function submit(event: FormEvent) { event.preventDefault(); const text = draft.trim(); if (!text || busy) return; setDraft(''); await onSend(text) }
  return <section className="chat-view">
    <div className="session-context"><span className="scope-pill">{session?.scope_mode === 'course' ? '课程会话' : '通用会话'}</span>{session?.scope_mode === 'general' && turnResolution?.sessionId === session.id && (turnResolution.status === 'resolved' ? <span>本轮解析到：{turnResolution.courseName ?? turnResolution.courseId}</span> : <span>本轮未解析到课程</span>)}{session?.scope_mode === 'general' && !turnResolution && session.resolved_course_id && <span>最近一次解析到：{session.course_name ?? session.resolved_course_id}</span>} {!session && <span>创建会话后，服务端会保存实际 scope 与课程解析结果。</span>}</div>
    <div className="messages" aria-live="polite">
      {!session && <div className="welcome"><span>✦</span><h1>今天想从哪里开始？</h1><p>在 {workspaceName} 中新建一个会话，课程解析、资料检索和引用都由服务端完成。</p><button className="primary-button" onClick={() => void onSend('我想开始学习')} disabled={busy}>开始对话</button></div>}
      {session && !messages.length && <div className="welcome"><span>◌</span><h1>{session.title || '新的学习会话'}</h1><p>问教材、做练习或制定计划。证据不足时 CoursePilot 会明确说明。</p></div>}
      {messages.filter(item => item.role !== 'system').map(message => <MessageCard message={message} key={message.id} onCitation={onCitation} />)}
    </div>
    <form className="composer-wrap" onSubmit={submit}><div className="composer"><textarea ref={composer} value={draft} onChange={event => setDraft(event.target.value)} placeholder={session ? '写下你的思路，或继续提问…' : '先新建一个会话…'} disabled={busy} aria-label="输入消息" rows={2} /><div className="composer-row"><span>仅文字与图片 · 图片 ≤ 10 MiB</span><button className="send-button" type="submit" disabled={!draft.trim() || busy} aria-label="发送消息">{busy ? '…' : '↑'}</button></div></div><p>回答优先依据当前课程的可检索资料；服务不可用时不会在本地伪造消息。</p></form>
  </section>
}

function MessageCard({ message, onCitation }: { message: Message; onCitation: (citation: Citation) => void }) {
  if (message.role === 'user') return <article className="message user-message"><div>{message.content}</div></article>
  const isInterrupted = message.artifact?.kind === 'interrupted' || message.status === 'interrupted'
  const resolution = message.resolution_status === 'resolved' ? `本轮解析：${message.resolved_course_name ?? message.resolved_course_id ?? '课程'}` : message.resolution_status ? '本轮未解析课程' : null
  return <article className="message assistant-message"><div className="agent-label"><span>CP</span><b>CoursePilot</b></div><div className="message-content">{message.content || <span className="typing">正在生成回答…</span>}</div>{resolution && <span className={`message-resolution ${message.resolution_status === 'resolved' ? 'resolved' : ''}`}>{resolution}</span>}{isInterrupted && <div className="interrupted">回答已中断。已生成的内容会保留，重新发送可继续学习。</div>}{message.citations && message.citations.length > 0 && <div className="citations">{message.citations.map((item, index) => <button key={`${item.id ?? item.chunk_id ?? index}`} onClick={() => onCitation(item)}>资料 {item.material_name ?? index + 1}{item.page ? ` · p.${item.page}` : ''}</button>)}</div>}{message.artifact && message.artifact.visibility !== 'model_private' && message.artifact.kind !== 'interrupted' && <div className="artifact-card"><b>公开学习内容</b><span>{message.artifact.kind}</span></div>}</article>
}

function LibraryView({ course, onCourseChange, onError }: { course: Course | null; onCourseChange: (course: Course) => void; onError: (message: string) => void }) {
  const [tab, setTab] = useState<'rag' | 'wiki'>('rag'); const [materials, setMaterials] = useState<Material[]>([]); const [jobs, setJobs] = useState<Record<string, Job>>({}); const [searchQuery, setSearchQuery] = useState(''); const [results, setResults] = useState<SearchResult[]>([]); const [loading, setLoading] = useState(false); const fileInput = useRef<HTMLInputElement>(null)
  const reload = async () => { if (!course) return; try { setMaterials(await api.materials(course.id)) } catch (error) { onError(errorText(error)) } }
  const indexedMaterials = materials.filter(item => (item.index_status ?? item.status) === 'indexed')
  useEffect(() => { setMaterials([]); setJobs({}); setResults([]); void reload() }, [course?.id])
  useEffect(() => { const active = Object.values(jobs).some(job => ['queued', 'running', 'pending'].includes(job.status)); if (!active) return; const interval = window.setInterval(() => { void (async () => { try { const entries = await Promise.all(Object.entries(jobs).map(async ([id]) => [id, await api.job(id)] as const)); setJobs(Object.fromEntries(entries)); await reload() } catch (error) { onError(errorText(error)) } })() }, 1500); return () => window.clearInterval(interval) }, [jobs])
  async function upload(event: ChangeEvent<HTMLInputElement>) { const file = event.target.files?.[0]; if (!file || !course) return; if (file.size > MAX_MATERIAL_BYTES) { onError('教材文件超过 100 MiB 上限。'); return } setLoading(true); try { const material = await api.uploadMaterial(course.id, file); setMaterials(current => [material, ...current]); const job = await api.indexMaterial(material.id); setJobs(current => ({ ...current, [job.id]: job })) } catch (error) { onError(errorText(error)) } finally { setLoading(false); event.target.value = '' } }
  async function toggleWiki() { if (!course) return; try { onCourseChange(await api.updateCourse(course.id, { wiki_enabled: !course.wiki_enabled })) } catch (error) { onError(errorText(error)) } }
  async function buildWiki(materialId: string) { try { const job = await api.buildWiki(materialId); setJobs(current => ({ ...current, [job.id]: job })) } catch (error) { onError(errorText(error)) } }
  async function search(event: FormEvent) { event.preventDefault(); if (!searchQuery.trim() || !course) return; setLoading(true); try { setResults(await api.search(course.id, searchQuery)) } catch (error) { onError(errorText(error)); setResults([]) } finally { setLoading(false) } }
  if (!course) return <EmptyCourseState />
  return <section className="page"><div className="page-inner"><div className="hero"><div><p className="eyebrow">{course.name}</p><h1>知识仓库</h1><p>RAG 资料库是默认入口；上传、索引和检索不依赖 Wiki。</p></div><div className="hero-actions"><button className="ghost-button" onClick={() => void reload()}>刷新状态</button></div></div><div className="tabs"><button className={tab === 'rag' ? 'active' : ''} onClick={() => setTab('rag')}>RAG 资料库</button><button className={tab === 'wiki' ? 'active' : ''} onClick={() => setTab('wiki')}>Wiki 知识页 {course.wiki_enabled ? '' : '（已关闭）'}</button></div>
    {tab === 'rag' ? <><div className="library-grid"><article className="card upload-card"><h2>上传教材</h2><p>支持 PDF、TXT、MD。上传后将依次校验、解析、切块并建立检索索引；本 Demo 使用 SQLite FTS/词项 fallback。</p><input ref={fileInput} type="file" accept=".pdf,.txt,.md,text/plain,application/pdf,text/markdown" onChange={upload} hidden /><button className="primary-button" onClick={() => fileInput.current?.click()} disabled={loading}>上传资料</button><small>单个教材 ≤ 100 MiB；对话图片仍为 ≤ 10 MiB，后端会再次校验。</small></article><article className="card search-card"><h2>检索验证</h2><p>在当前课程范围内查询，确认索引质量和可引用片段。</p><form onSubmit={search}><input value={searchQuery} onChange={event => setSearchQuery(event.target.value)} placeholder="例如：链式法则" /><button className="primary-button" disabled={loading}>检索</button></form></article></div><article className="card material-card"><div className="card-heading"><div><h2>资料与索引</h2><p>每一项状态来自后端 job，不在浏览器模拟进度。</p></div><button className="text-button" onClick={() => void reload()}>刷新</button></div>{materials.length ? materials.map(material => <MaterialRow material={material} jobs={jobs} key={material.id} />) : <div className="empty-inline">尚未上传资料。上传并完成索引后，即可在此验证检索结果。</div>}</article>{results.length > 0 && <article className="card results-card"><h2>检索结果</h2>{results.map((result, index) => <div className="result" key={result.id ?? result.chunk_id ?? index}><b>{result.material_name ?? '资料片段'} {result.page ? `· p.${result.page}` : ''}</b><p>{result.text ?? '服务端未返回可展示的文本片段。'}</p><small>{result.score !== undefined ? `检索排序分 ${result.score.toFixed(4)}` : '已返回引用'}</small></div>)}</article>}</> : <article className="card wiki-card"><div className="switch-row"><div><h2>启用 Course Wiki <span>实验功能</span></h2><p>关闭时不触发教材解析，不影响 RAG 检索或 Tutor；关闭不会删除既有页面。</p></div><button className={`switch ${course.wiki_enabled ? 'on' : ''}`} aria-label="切换 Course Wiki" onClick={toggleWiki}><i /></button></div>{course.wiki_enabled ? <><p className="wiki-note">选择已完成索引的资料，显式启动“提取目录 → 概念候选 → 页面草稿 → 待确认”。</p>{indexedMaterials.length ? indexedMaterials.map(material => <div className="material-row" key={material.id}><div className="file-mark">{fileKind(material)}</div><div><b>{material.filename ?? material.name ?? '未命名资料'}</b><small>已索引，可独立解析到 Wiki</small></div><button className="ghost-button" onClick={() => void buildWiki(material.id)}>解析到 Wiki</button></div>) : <div className="empty-inline">请先上传并完成至少一份资料的索引。</div>}</> : <div className="empty-inline"><b>Wiki 尚未启用</b><p>它用于浏览和检查教材生成的知识页；RAG 资料库仍可完整使用。</p></div>}</article>}</div></section>
}

function MaterialRow({ material, jobs }: { material: Material; jobs: Record<string, Job> }) { const job = Object.values(jobs).find(item => item.material_id === material.id); const status = job?.stage ?? job?.status ?? material.index_status ?? material.status ?? '等待索引'; return <div className="material-row"><div className="file-mark">{fileKind(material)}</div><div className="material-copy"><b>{material.filename ?? material.name ?? '未命名资料'}</b><small>{[material.pages ? `${material.pages} 页` : null, material.size_bytes ? `${Math.ceil(material.size_bytes / 1024)} KiB` : null, status].filter(Boolean).join(' · ')}</small>{job && <div className="job-progress"><i style={{ width: `${job.progress ?? 15}%` }} /></div>}{material.error && <small className="danger-text">{material.error}</small>}</div><span className={`status-tag ${String(status).toLowerCase().includes('fail') ? 'failed' : ''}`}>{status}</span></div> }
function fileKind(material: Material) { const name = material.filename ?? material.name ?? ''; return name.split('.').pop()?.toUpperCase().slice(0, 4) || 'FILE' }

function PlanView({ course, onError }: { course: Course | null; onError: (message: string) => void }) {
  const [plan, setPlan] = useState<Plan | null>(null)
  const [loaded, setLoaded] = useState(false)
  useEffect(() => {
    setPlan(null); setLoaded(false)
    if (!course) return
    api.plan(course.id).then(payload => { setPlan(payload.plan); setLoaded(true) }).catch(error => onError(errorText(error)))
  }, [course?.id])
  if (!course) return <EmptyCourseState />
  return <section className="page"><div className="page-inner">
    <div className="hero"><div><p className="eyebrow">{course.name}</p><h1>学习计划</h1><p>计划由服务端持久化并逐版本演化；修改未来条目需要确认，历史条目只读。</p></div></div>
    {!loaded ? <p className="mini-empty">正在读取计划…</p> : plan ? <article className="card"><div className="card-heading"><div><h2>当前计划</h2><p>版本 v{plan.version} · {plan.items.length} 个条目</p></div></div>{plan.items.map(item => <div className="material-row" key={item.id}><div className="file-mark">{item.due_date.slice(5)}</div><div className="material-copy"><b>{item.title}</b><small>{item.status}</small></div></div>)}</article> : <article className="card"><h2>还没有学习计划</h2><p>该课程尚未创建计划。生成与调整计划的写接口将随规划功能开放；此页读取的是服务端持久化状态，不展示本地虚构数据。</p></article>}
  </div></section>
}
function ArchiveView({ course, onError }: { course: Course | null; onError: (message: string) => void }) {
  const [archive, setArchive] = useState<ArchiveSummary | null>(null)
  useEffect(() => {
    setArchive(null)
    if (!course) return
    api.archive(course.id).then(setArchive).catch(error => onError(errorText(error)))
  }, [course?.id])
  if (!course) return <EmptyCourseState />
  return <section className="page"><div className="page-inner">
    <div className="hero"><div><p className="eyebrow">{course.name}</p><h1>学习档案</h1><p>掌握度由 append-only 证据事件流投影而来；此页展示服务端已持久化的事件。</p></div></div>
    {!archive ? <p className="mini-empty">正在读取档案…</p> : <article className="card"><div className="card-heading"><div><h2>证据事件</h2><p>共 {archive.evidence_count} 条</p></div></div>{archive.events.length ? archive.events.map(event => <div className="material-row" key={event.id}><div className="file-mark">{event.kind.toUpperCase().slice(0, 4)}</div><div className="material-copy"><b>{event.concept_id ?? event.topic_hint ?? '未归因'}</b><small>{event.attribution_status} · {timeLabel(event.created_at)}</small></div></div>) : <div className="empty-inline">还没有证据事件。答题、小测与纠错发生后，这里会出现可追溯的记录。</div>}</article>}
  </div></section>
}

function SettingsView({ courses, onError }: { courses: Course[]; onError: (message: string) => void }) { const [health, setHealth] = useState<Record<string, unknown> | null>(null); const [loading, setLoading] = useState(false); async function check() { setLoading(true); try { setHealth(await api.health()) } catch (error) { onError(errorText(error)) } finally { setLoading(false) } } const llm = health?.llm ?? health?.llm_status ?? '未检查'; const rag = health?.rag ?? health?.rag_backend ?? '未检查'; return <section className="page"><div className="page-inner"><div className="hero"><div><h1>管理与设置</h1><p>课程、服务能力与后续的 Skills、飞书渠道设置分开管理。</p></div><button className="ghost-button" onClick={check} disabled={loading}>检查服务</button></div><div className="settings-grid"><article className="card"><h2>课程与教材</h2><p>共 {courses.length} 门课程。课程颜色由服务端稳定返回。</p>{courses.length ? courses.map(course => <div className="settings-course" key={course.id}><i style={{ backgroundColor: course.color }} /><b>{course.name}</b><span>{course.wiki_enabled ? 'Wiki 已开启' : 'Wiki 已关闭'}</span></div>) : <p className="empty-inline">暂无课程，请从左栏创建。</p>}</article><article className="card"><h2>Skills</h2><p>Skill 上传与安装接口尚未列入 2.0 Demo API 契约。上传能力默认保持关闭，避免前端伪造安装状态。</p><button className="ghost-button" disabled>上传 Skill（等待接口）</button></article><article className="card"><h2>飞书渠道</h2><p>首版只有飞书渠道；飞书始终使用一个通用会话，不提供课程选择。密钥绝不在前端回显。</p><button className="ghost-button" disabled>配置飞书（等待接口）</button></article><article className="card health-card"><h2>运行状态</h2>{health ? <><dl><div><dt>LLM</dt><dd>{typeof llm === 'object' ? JSON.stringify(llm) : String(llm)} {String(llm).includes('demo_fallback') ? '· Demo fallback' : ''}</dd></div><div><dt>RAG backend</dt><dd>{typeof rag === 'object' ? JSON.stringify(rag) : String(rag)}</dd></div></dl><pre>{JSON.stringify(health, null, 2)}</pre></> : <p>点击“检查服务”查看 LLM 的 demo_fallback 状态与 RAG backend。</p>}</article></div></div></section> }
function EmptyCourseState() { return <section className="page"><div className="page-inner empty-course"><span>▤</span><h1>选择一个课程工作区</h1><p>课程资料、RAG 索引与 Wiki 均以课程为边界。先从左侧创建或选择课程。</p></div></section> }
function CitationDrawer({ citation, onClose }: { citation: Citation; onClose: () => void }) { return <aside className="citation-drawer" role="dialog" aria-label="教材引用详情"><header><div><p>教材引用</p><h2>{citation.material_name ?? '资料片段'}</h2></div><button aria-label="关闭引用详情" onClick={onClose}>×</button></header><p className="citation-location">{citation.page ? `第 ${citation.page} 页` : citation.chunk_id ? `片段 ${citation.chunk_id}` : '服务端返回的资料定位'}</p><blockquote>{citation.text ?? '该引用未提供可展示的原文片段。'}</blockquote>{citation.score !== undefined && <p>检索排序分：{citation.score.toFixed(4)}</p>}</aside> }
