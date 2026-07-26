import { ChangeEvent, FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
import { ApiError, api } from './api'
import type { ArchiveSummary, Attachment, Citation, ContextUsage, Course, Job, Material, Message, Plan, ScopeMode, SearchResult, SessionSummary, ToolActivity } from './types'

type View = 'chat' | 'library' | 'plan' | 'archive' | 'settings'
type Workspace = { scope: ScopeMode; courseId?: string }
type TurnResolution = { sessionId: string; status: string; courseId: string | null; courseName: string | null }

const viewNames: Record<View, string> = { chat: '对话', library: '知识仓库', plan: '学习计划', archive: '学习档案', settings: '管理与设置' }
const nav: { id: View; num: string }[] = [
  { id: 'chat', num: '01' }, { id: 'library', num: '02' }, { id: 'plan', num: '03' }, { id: 'archive', num: '04' },
]
const MAX_MATERIAL_BYTES = 100 * 1024 * 1024
const TOOL_LABELS: Record<string, string> = {
  search_materials: '检索教材', list_materials: '资料清单', get_plan: '学习计划', plan_update: '写入计划',
  get_archive: '学习档案', concept_search: '概念目录', emit_evidence: '记录学习证据', memory_patch: '更新记忆',
  use_skill: '加载能力', artifact_read: '读取练习', artifact_append: '保存练习',
}

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
  const [health, setHealth] = useState<Record<string, unknown> | null>(null)
  const [citation, setCitation] = useState<Citation | null>(null)
  const [turnResolution, setTurnResolution] = useState<TurnResolution | null>(null)
  // 上下文构成来自服务端实际组装结果；换会话就清空，避免显示上一会话的数字。
  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(null)

  const course = useMemo(() => courses.find(item => item.id === workspace.courseId) ?? null, [courses, workspace.courseId])
  const heading = activeSession?.title && view === 'chat' ? activeSession.title : viewNames[view]

  useEffect(() => { localStorage.setItem('cp-sidebar-collapsed', String(sidebarCollapsed)) }, [sidebarCollapsed])
  useEffect(() => {
    api.health().then(payload => { setApiOnline(true); setHealth(payload) }).catch(() => setApiOnline(false))
    api.courses().then(setCourses).catch(error => setNotice(errorText(error)))
  }, [])
  useEffect(() => { void loadSessions() }, [workspace.scope, workspace.courseId])
  useEffect(() => { setTurnResolution(null); setContextUsage(null); if (activeSession) void loadMessages(activeSession.id) }, [activeSession?.id])

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
    setWorkspace(next); setView('chat'); setSidebarOpen(false); setCitation(null); setTurnResolution(null); setContextUsage(null)
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
  const healthLlm = (health?.llm ?? null) as Record<string, unknown> | null
  const healthRag = (health?.rag ?? null) as Record<string, unknown> | null
  return <div className={`app ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
    {sidebarOpen && <button className="sidebar-backdrop" aria-label="关闭导航" onClick={() => setSidebarOpen(false)} />}
    <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`} aria-label="课程与会话">
      <div className="brand"><div className="brandmark">{'>_'}</div><div className="brand-copy"><strong>CoursePilot</strong><span className="ver">v2.0</span></div></div>
      <div className="side-label">WORKSPACE</div>
      <button className={`workspace-card ${workspace.scope === 'general' ? 'selected' : ''}`} onClick={() => switchWorkspace({ scope: 'general' })}>
        <span className="general-icon" aria-hidden><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><circle cx="12" cy="12" r="9" /><path d="M12 3v18M3 12h18" opacity=".35" /></svg></span>
        <span className="workspace-copy"><b>通用模式</b><small>每轮按问题解析课程</small></span>
      </button>
      <div className="course-switcher">
        {courses.map(item => <button className={`course-choice ${item.id === workspace.courseId ? 'selected' : ''}`} key={item.id} onClick={() => switchWorkspace({ scope: 'course', courseId: item.id })}>
          <i style={{ backgroundColor: item.color }} /><span>{item.name}</span>{item.wiki_enabled && <em>Wiki</em>}
        </button>)}
        <button className="text-button add-course" onClick={createCourse} disabled={busy}>＋ 新建课程</button>
      </div>
      <div className="side-label">NAV</div>
      <nav className="main-nav" aria-label="学习导航">
        {nav.map(item => <button className={view === item.id ? 'active' : ''} key={item.id} onClick={() => { setView(item.id); setSidebarOpen(false) }}><span aria-hidden>{item.num}</span><b>{viewNames[item.id]}</b></button>)}
      </nav>
      <div className="sessions-head"><span>SESSIONS</span><button aria-label="新建会话" onClick={newSession} disabled={busy}>＋</button></div>
      <div className="session-list">
        {sessions.length ? sessions.map(session => <button className={`session ${session.id === activeSession?.id ? 'active' : ''}`} key={session.id} onClick={() => { setActiveSession(session); setView('chat'); setSidebarOpen(false) }}>
          <i title={session.scope_mode === 'general' ? '通用会话' : '课程会话'} style={{ backgroundColor: session.course_color ?? '#D4D4D8' }} /><span className="session-text"><b>{session.title || '未命名会话'}</b><small>{timeLabel(session.updated_at)}</small></span>
        </button>) : <p className="mini-empty">此工作区还没有会话。</p>}
      </div>
      <button className="new-session" onClick={newSession} disabled={busy}>＋ 新建{workspace.scope === 'general' ? '通用' : '课程'}会话</button>
      <div className="sidebar-foot"><button onClick={() => { setView('settings'); setSidebarOpen(false) }}>⚙ <span>管理与设置</span></button></div>
    </aside>
    <main className="main">
      <header className="topbar">
        <button className="icon-button mobile-only" aria-label="打开导航" onClick={() => setSidebarOpen(true)}>☰</button>
        <button className="icon-button collapse-only" aria-label="折叠侧栏" onClick={() => setSidebarCollapsed(value => !value)}>☷</button>
        <div className="title-area"><b>{heading}</b><span className="crumb"><i style={{ backgroundColor: course?.color ?? '#D4D4D8' }} /> {workspaceName}</span></div>
      </header>
      {notice && <div className="notice" role="alert"><span>{notice}</span><button aria-label="关闭错误提示" onClick={() => setNotice('')}>×</button></div>}
      {view === 'chat' && <ChatView session={activeSession} messages={messages} workspaceName={workspaceName} scope={workspace.scope} turnResolution={turnResolution} contextUsage={contextUsage} onCitation={setCitation} onUpload={async file => {
        try {
          let targetSession = activeSession
          if (!targetSession) {
            targetSession = await api.createSession(workspace.scope, workspace.courseId)
            setSessions(current => [targetSession!, ...current]); setActiveSession(targetSession); setMessages([])
          }
          return await api.uploadAttachment(targetSession.id, file)
        } catch (error) { setNotice(errorText(error)); throw error }
      }} onSend={async (content, attachmentIds) => {
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
        const activity: ToolActivity[] = []
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
          if (payload.type === 'context_usage' && payload.segments) {
            setContextUsage({ segments: payload.segments, total_chars: payload.total_chars ?? 0, limit_chars: payload.limit_chars ?? 1, history_budget_chars: payload.history_budget_chars ?? 0, dropped_history: payload.dropped_history ?? 0, clipped_history: payload.clipped_history ?? 0 })
          }
          if (payload.type === 'tool_call' && payload.call_id) {
            activity.push({ call_id: payload.call_id, name: payload.name ?? '工具', origin: payload.origin })
            setMessages(current => current.map(item => item.id === pendingId ? { ...item, activity: [...activity] } : item))
          }
          if (payload.type === 'tool_result' && payload.call_id) {
            const entry = activity.find(item => item.call_id === payload.call_id)
            if (entry) { entry.summary = payload.summary; entry.ok = payload.ok }
            setMessages(current => current.map(item => item.id === pendingId ? { ...item, activity: [...activity] } : item))
          }
          const delta = payload.delta ?? payload.content ?? payload.text ?? ''
          if (delta) setMessages(current => current.map(item => item.id === pendingId ? { ...item, content: item.content + delta } : item))
          if (payload.type === 'turn_completed' && payload.finish_reason === 'length') setNotice('回答达到长度上限，内容可能不完整。')
        }, attachmentIds); await loadMessages(targetSession.id); await loadSessions() }
        catch (error) {
          setNotice(errorText(error))
          // 优先回读服务端真值（部分回答已带 interrupted 状态持久化）；服务不可达时保留本地标记。
          try { await loadMessages(targetSession.id); await loadSessions() }
          catch { setMessages(current => current.map(item => item.id === pendingId ? { ...item, content: item.content || '本次回答未能完成。', artifact: { kind: 'interrupted' } } : item)) }
        }
        finally { setBusy(false) }
      }} busy={busy} />}
      {view !== 'chat' && view !== 'settings' && !course && <CoursePickerState view={view} courses={courses} onPick={courseId => switchWorkspace({ scope: 'course', courseId })} onCreate={createCourse} />}
      {view === 'library' && course && <LibraryView course={course} onCourseChange={updated => setCourses(current => current.map(item => item.id === updated.id ? updated : item))} onError={setNotice} />}
      {view === 'plan' && course && <PlanView course={course} onError={setNotice} />}
      {view === 'archive' && course && <ArchiveView course={course} onError={setNotice} />}
      {view === 'settings' && <SettingsView courses={courses} onError={setNotice} />}
      <footer className="statusbar">
        <span className={apiOnline ? 'ok' : 'bad'}>● {apiOnline ? 'connected' : 'offline'}</span>
        {healthLlm && <span>{String(healthLlm.provider)}/{String(healthLlm.model)}{healthLlm.enabled ? '' : ' · local demo'}</span>}
        {healthRag && <span>retrieval: {String(healthRag.backend)}</span>}
        <span className="right">CoursePilot v2.0</span>
      </footer>
    </main>
    {citation && <CitationDrawer citation={citation} onClose={() => setCitation(null)} />}
  </div>
}

function ChatView({ session, messages, workspaceName, scope, turnResolution, contextUsage, onCitation, onUpload, onSend, busy }: { session: SessionSummary | null; messages: Message[]; workspaceName: string; scope: ScopeMode; turnResolution: TurnResolution | null; contextUsage: ContextUsage | null; onCitation: (citation: Citation) => void; onUpload: (file: File) => Promise<Attachment>; onSend: (content: string, attachmentIds: string[]) => Promise<void>; busy: boolean }) {
  const [draft, setDraft] = useState(''); const composer = useRef<HTMLTextAreaElement>(null)
  const [attachments, setAttachments] = useState<Attachment[]>([]); const [uploading, setUploading] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const isCourseScope = session ? session.scope_mode === 'course' : scope === 'course'
  // 切换会话时丢弃不属于新会话的附件；上传自动建会话的场景附件归属一致，不受影响。
  useEffect(() => { setAttachments(current => current.filter(item => item.session_id === session?.id)) }, [session?.id])
  async function pickFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]; event.target.value = ''
    if (!file) return
    setUploading(true)
    try { const attachment = await onUpload(file); setAttachments(current => [...current, attachment]) }
    catch { /* 错误提示由上层统一展示 */ }
    finally { setUploading(false) }
  }
  async function submit(event?: { preventDefault(): void }) {
    event?.preventDefault(); const text = draft.trim()
    if (!text || busy || uploading) return
    const ids = attachments.map(item => item.id)
    setDraft(''); setAttachments([]); await onSend(text, ids)
  }
  return <section className="chat-view">
    <div className="session-context">
      <span className="scope-pill">{session?.scope_mode === 'course' ? '课程会话' : '通用会话'}</span>
      {session?.scope_mode === 'course' && <span className="context-course"><i style={{ backgroundColor: session.course_color ?? '#99A19D' }} />{session.course_name ?? '（课程已删除）'}</span>}
      {session?.scope_mode === 'general' && turnResolution?.sessionId === session.id && (turnResolution.status === 'resolved' ? <span>本轮解析到：{turnResolution.courseName ?? turnResolution.courseId}</span> : <span>本轮未解析到课程 · 在问题中说明课程名即可</span>)}
      {session?.scope_mode === 'general' && !turnResolution && session.resolved_course_id && <span>最近解析到：{session.course_name ?? session.resolved_course_id}</span>}
      {!session && <span>发送第一条消息会自动创建会话。</span>}
    </div>
    <div className="messages" aria-live="polite">
      {!messages.length && <div className="welcome"><span aria-hidden>❯</span><h1>今天想从哪里开始？</h1><p>{isCourseScope ? `这里的提问固定使用「${workspaceName}」的资料，回答会带教材页码引用。` : '通用模式会按每轮问题解析课程；直接提到课程名（如某门课的某个概念）解析最可靠。'}</p><div className="suggestion-row">{(isCourseScope ? ['讲讲这门课的核心概念', '给我出几道练习题', '帮我制定复习计划'] : ['「课程名」的某个概念怎么理解？', '给我出几道练习题', '帮我制定复习计划']).map(text => <button key={text} className="suggestion-chip" onClick={() => { setDraft(text); composer.current?.focus() }}>{text}</button>)}</div></div>}
      {messages.filter(item => item.role !== 'system').map(message => <MessageCard message={message} key={message.id} onCitation={onCitation} showResolution={!isCourseScope} />)}
    </div>
    <form className="composer-wrap" onSubmit={submit}>
      {contextUsage && <ContextMeter usage={contextUsage} />}
      {(attachments.length > 0 || uploading) && <div className="attach-list">
        {attachments.map(item => <div className={item.needs_confirmation ? 'attach-chip warn' : 'attach-chip'} key={item.id}>
          <span className="attach-name">IMG · {item.filename}</span>
          <span className="attach-preview">{item.needs_confirmation ? '未识别出文字，发送前请在消息里补充说明' : item.transcription}</span>
          <button type="button" aria-label={`移除图片 ${item.filename}`} onClick={() => setAttachments(current => current.filter(other => other.id !== item.id))}>×</button>
        </div>)}
        {uploading && <div className="attach-chip pending"><span className="attach-name">IMG</span><span className="attach-preview">正在转录图片文字…</span></div>}
      </div>}
      <div className="composer"><span className="prompt" aria-hidden>❯</span><textarea ref={composer} value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit() } }} placeholder={session ? '写下你的思路，或继续提问…' : '先新建一个会话…'} disabled={busy} aria-label="输入消息" rows={2} /><div className="composer-row"><button type="button" className="attach-button" onClick={() => fileInput.current?.click()} disabled={busy || uploading} aria-label="上传图片提问"><svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden><rect x="1.5" y="2.5" width="13" height="11" rx="1.5" /><circle cx="5.5" cy="6.5" r="1.2" /><path d="M2.5 12.5 6.5 9l3 2.5 2-1.5 2 2" /></svg>图片</button><span>Enter 发送 · Shift+Enter 换行 · 图片 ≤ 10 MiB</span><button className="send-button" type="submit" disabled={!draft.trim() || busy || uploading} aria-label="发送消息">{busy ? '…' : '↑'}</button></div></div>
      <input ref={fileInput} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={pickFile} />
      <p>回答优先依据当前课程的可检索资料；没有命中教材时会明确标注“以下不是当前教材结论”。</p></form>
  </section>
}

function ContextMeter({ usage }: { usage: ContextUsage }) {
  const [open, setOpen] = useState(false)
  const k = (chars: number) => chars >= 1000 ? `${(chars / 1000).toFixed(1)}K` : String(chars)
  const percent = Math.min(100, Math.round((usage.total_chars / usage.limit_chars) * 100))
  const filled = Math.round(percent / 5)
  return <div className="context-meter">
    <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}>
      <span className="meter-bar" aria-hidden>{'▓'.repeat(filled)}{'░'.repeat(20 - filled)}</span>
      <span className="meter-total">{percent}% · {k(usage.total_chars)} / {k(usage.limit_chars)}</span>
      <span className="meter-hint">上下文 · 字符数估算</span>
    </button>
    {open && <div className="meter-detail">
      {usage.segments.map(segment => <div key={segment.label}><span>{segment.label}</span><b>{k(segment.chars)}</b></div>)}
      <p>按字符数近似 token（未接真实 tokenizer，实际占用通常更小）。历史预算 {k(usage.history_budget_chars)}。</p>
      {usage.dropped_history > 0 && <p className="meter-warn">更早的 {usage.dropped_history} 条消息未进入本轮上下文。</p>}
      {usage.clipped_history > 0 && <p className="meter-warn">有 {usage.clipped_history} 条超长消息被截断后才进入上下文。</p>}
    </div>}
  </div>
}

function MessageCard({ message, onCitation, showResolution }: { message: Message; onCitation: (citation: Citation) => void; showResolution: boolean }) {
  if (message.role === 'user') return <article className="message user-message"><div>{message.content}</div></article>
  const isInterrupted = message.artifact?.kind === 'interrupted' || message.status === 'interrupted'
  // 课程会话的课程是固定的，逐条标注解析结果只会制造噪音；仅通用会话展示。
  const resolution = !showResolution ? null : message.resolution_status === 'resolved' ? `本轮解析：${message.resolved_course_name ?? message.resolved_course_id ?? '课程'}` : message.resolution_status ? '本轮未解析课程' : null
  return <article className="message assistant-message"><div className="agent-label"><span aria-hidden>❯</span><b>CoursePilot</b></div>{message.activity && message.activity.length > 0 && <div className="tool-activity">{message.activity.map(entry => <span key={entry.call_id} className={`tool-chip ${entry.ok === false ? 'warn' : ''} ${entry.summary ? 'done' : 'pending'}`}><i aria-hidden>{entry.summary ? (entry.ok === false ? '×' : '✓') : '…'}</i>{TOOL_LABELS[entry.name] ?? entry.name}{entry.summary ? ` · ${entry.summary}` : ''}</span>)}</div>}<div className="message-content">{message.content ? <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>{message.content}</ReactMarkdown> : <span className="typing">正在生成回答…</span>}</div>{resolution && <span className={`message-resolution ${message.resolution_status === 'resolved' ? 'resolved' : ''}`}>{resolution}</span>}{isInterrupted && <div className="interrupted">回答已中断。已生成的内容会保留，重新发送可继续学习。</div>}{message.citations && message.citations.length > 0 && <div className="citations"><span className="refs-label">SOURCES · {message.citations.length}</span>{message.citations.map((item, index) => <button key={`${item.id ?? item.chunk_id ?? index}`} onClick={() => onCitation(item)}><i>[{item.number ?? index + 1}]</i>{item.material_name ?? '资料'}{item.page ? `:${item.page}` : ''}</button>)}</div>}{message.artifact && message.artifact.visibility !== 'model_private' && message.artifact.kind !== 'interrupted' && <div className="artifact-card"><b>公开学习内容</b><span>{message.artifact.kind}</span></div>}</article>
}

function LibraryView({ course, onCourseChange, onError }: { course: Course; onCourseChange: (course: Course) => void; onError: (message: string) => void }) {
  const [tab, setTab] = useState<'rag' | 'wiki'>('rag'); const [materials, setMaterials] = useState<Material[]>([]); const [jobs, setJobs] = useState<Record<string, Job>>({}); const [searchQuery, setSearchQuery] = useState(''); const [results, setResults] = useState<SearchResult[]>([]); const [loading, setLoading] = useState(false); const fileInput = useRef<HTMLInputElement>(null)
  const [ragBackend, setRagBackend] = useState<string>('')
  const reload = async () => { try { setMaterials(await api.materials(course.id)) } catch (error) { onError(errorText(error)) } }
  const indexedMaterials = materials.filter(item => (item.index_status ?? item.status) === 'indexed')
  useEffect(() => { api.health().then(payload => setRagBackend(((payload.rag as Record<string, unknown>)?.backend as string) ?? '')).catch(() => {}) }, [])
  useEffect(() => { setMaterials([]); setJobs({}); setResults([]); void reload() }, [course.id])
  useEffect(() => { const active = Object.values(jobs).some(job => ['queued', 'running', 'pending'].includes(job.status)); if (!active) return; const interval = window.setInterval(() => { void (async () => { try { const entries = await Promise.all(Object.entries(jobs).map(async ([id]) => [id, await api.job(id)] as const)); setJobs(Object.fromEntries(entries)); await reload() } catch (error) { onError(errorText(error)) } })() }, 1500); return () => window.clearInterval(interval) }, [jobs])
  async function upload(event: ChangeEvent<HTMLInputElement>) { const file = event.target.files?.[0]; if (!file) return; if (file.size > MAX_MATERIAL_BYTES) { onError('教材文件超过 100 MiB 上限。'); return } setLoading(true); try { const material = await api.uploadMaterial(course.id, file); setMaterials(current => [material, ...current]); const job = await api.indexMaterial(material.id); setJobs(current => ({ ...current, [job.id]: job })) } catch (error) { onError(errorText(error)) } finally { setLoading(false); event.target.value = '' } }
  async function toggleWiki() { try { onCourseChange(await api.updateCourse(course.id, { wiki_enabled: !course.wiki_enabled })) } catch (error) { onError(errorText(error)) } }
  async function reindex(materialId: string) { try { const job = await api.indexMaterial(materialId); setJobs(current => ({ ...current, [job.id]: job })) } catch (error) { onError(errorText(error)) } }
  async function buildWiki(materialId: string) { try { const job = await api.buildWiki(materialId); setJobs(current => ({ ...current, [job.id]: job })) } catch (error) { onError(errorText(error)) } }
  async function search(event: FormEvent) { event.preventDefault(); if (!searchQuery.trim()) return; setLoading(true); try { setResults(await api.search(course.id, searchQuery)) } catch (error) { onError(errorText(error)); setResults([]) } finally { setLoading(false) } }
  const backendLabel = ragBackend === 'hybrid_bge' ? '语义 + 词面混合检索' : ragBackend ? '仅词面检索（语义向量未启用）' : ''
  return <section className="page"><div className="page-inner"><div className="hero"><div><p className="eyebrow">知识仓库</p><h1 className="course-heading"><i style={{ backgroundColor: course.color }} />{course.name}</h1><p>这门课程的教材、索引与检索都在这里；切换课程请使用左栏工作区。{backendLabel && <span className="backend-badge">{backendLabel}</span>}</p></div><div className="hero-actions"><button className="ghost-button" onClick={() => void reload()}>刷新状态</button></div></div><div className="tabs"><button className={tab === 'rag' ? 'active' : ''} onClick={() => setTab('rag')}>RAG 资料库</button><button className={tab === 'wiki' ? 'active' : ''} onClick={() => setTab('wiki')}>Wiki 知识页 {course.wiki_enabled ? '' : '（已关闭）'}</button></div>
    {tab === 'rag' ? <><div className="library-grid"><article className="card upload-card"><h2>上传教材</h2><p>支持 PDF、TXT、MD。上传后自动执行：解析文本 → 切块 → 生成语义向量 → 建立索引。</p><input ref={fileInput} type="file" accept=".pdf,.txt,.md,text/plain,application/pdf,text/markdown" onChange={upload} hidden /><button className="primary-button" onClick={() => fileInput.current?.click()} disabled={loading}>上传到「{course.name}」</button><small>单个教材 ≤ 100 MiB；对话图片仍为 ≤ 10 MiB，后端会再次校验。</small></article><article className="card search-card"><h2>检索验证</h2><p>在「{course.name}」范围内试查，确认索引质量与可引用片段。</p><form onSubmit={search}><input value={searchQuery} onChange={event => setSearchQuery(event.target.value)} placeholder="试试概念名或一个真实问题" /><button className="primary-button" disabled={loading}>检索</button></form></article></div><article className="card material-card"><div className="card-heading"><div><h2>资料与索引</h2><p>状态来自后端 job，不在浏览器模拟进度。</p></div><button className="text-button" onClick={() => void reload()}>刷新</button></div>{materials.length ? materials.map(material => <MaterialRow material={material} jobs={jobs} key={material.id} onReindex={reindex} />) : <div className="empty-inline">尚未上传资料。上传并完成索引后，即可在此验证检索结果。</div>}</article>{results.length > 0 && <article className="card results-card"><h2>检索结果</h2>{results.map((result, index) => <div className="result" key={result.id ?? result.chunk_id ?? index}><b>{result.material_name ?? '资料片段'} {result.page ? `· p.${result.page}` : ''}</b><p>{result.text ?? '服务端未返回可展示的文本片段。'}</p><small>{result.score !== undefined ? `检索排序分 ${result.score.toFixed(4)}` : '已返回引用'}</small></div>)}</article>}</> : <article className="card wiki-card"><div className="switch-row"><div><h2>启用 Course Wiki <span>实验功能</span></h2><p>关闭时不触发教材解析，不影响 RAG 检索或 Tutor；关闭不会删除既有页面。</p></div><button className={`switch ${course.wiki_enabled ? 'on' : ''}`} aria-label="切换 Course Wiki" onClick={toggleWiki}><i /></button></div>{course.wiki_enabled ? <><p className="wiki-note">选择已完成索引的资料，显式启动“提取目录 → 概念候选 → 页面草稿 → 待确认”。</p>{indexedMaterials.length ? indexedMaterials.map(material => {
      const wikiJob = Object.values(jobs).find(item => item.material_id === material.id && item.type === 'wiki')
      const running = wikiJob ? !['completed', 'failed'].includes(wikiJob.status) : false
      return <div className="material-row" key={material.id}><div className="file-mark">{fileKind(material)}</div><div className="material-copy"><b>{material.filename ?? material.name ?? '未命名资料'}</b><small>{wikiJob ? (STAGE_LABELS[String(wikiJob.stage ?? wikiJob.status)] ?? String(wikiJob.status)) : '已索引，可独立解析到 Wiki'}</small>{wikiJob && <div className="job-progress"><i style={{ width: `${wikiJob.progress ?? 15}%` }} /></div>}{wikiJob?.error && <small className="danger-text">{wikiJob.error}</small>}</div><button className="ghost-button" onClick={() => void buildWiki(material.id)} disabled={running}>{wikiJob && !running ? '重新解析到 Wiki' : '解析到 Wiki'}</button></div>
    }) : <div className="empty-inline">请先上传并完成至少一份资料的索引。</div>}</> : <div className="empty-inline"><b>Wiki 尚未启用</b><p>它用于浏览和检查教材生成的知识页；RAG 资料库仍可完整使用。</p></div>}</article>}</div></section>
}

const STAGE_LABELS: Record<string, string> = { uploaded: '待索引', queued: '排队中', starting: '准备中', extracting: '解析文本', chunking: '切块', embedding: '生成语义向量', indexing: '建立索引', completed: '已索引', indexed: '已索引', indexing_failed: '失败', failed: '失败', reading_index: '读取索引', wiki_completed: 'Wiki 已生成' }
const INDEX_PIPELINE: [string, string][] = [['extracting', '解析'], ['chunking', '切块'], ['embedding', '向量'], ['indexing', '索引']]

function MaterialRow({ material, jobs, onReindex }: { material: Material; jobs: Record<string, Job>; onReindex: (materialId: string) => void }) {
  const job = Object.values(jobs).find(item => item.material_id === material.id)
  const rawStatus = job?.stage ?? job?.status ?? material.index_status ?? material.status ?? 'uploaded'
  const statusLabel = STAGE_LABELS[String(rawStatus)] ?? String(rawStatus)
  const failed = String(job?.status ?? rawStatus).toLowerCase().includes('fail')
  const jobActive = job ? !['completed', 'failed'].includes(job.status) : false
  const indexed = (material.index_status ?? material.status) === 'indexed'
  const semantic = (material.embedded_count ?? 0) > 0
  const stageIndex = INDEX_PIPELINE.findIndex(([stage]) => stage === job?.stage)
  const productSummary = indexed && !jobActive
    ? `${material.chunk_count ?? 0} 块 · ${semantic ? '语义 + 词面检索就绪' : '仅词面（点「重建索引」补语义向量）'}`
    : null
  return <div className="material-row">
    <div className="file-mark">{fileKind(material)}</div>
    <div className="material-copy">
      <b>{material.filename ?? material.name ?? '未命名资料'}</b>
      <small>{[material.size_bytes ? `${Math.ceil(material.size_bytes / 1024 / 1024)} MiB` : null, productSummary].filter(Boolean).join(' · ') || statusLabel}</small>
      {jobActive && job?.type !== 'wiki' && <div className="pipeline">{INDEX_PIPELINE.map(([stage, label], position) => <span key={stage} className={`pipeline-step ${stageIndex > position ? 'done' : stageIndex === position ? 'current' : ''}`}>{label}</span>)}</div>}
      {job && <div className="job-progress"><i style={{ width: `${job.progress ?? 15}%` }} /></div>}
      {failed && job?.error && <small className="danger-text">{job.error}</small>}
    </div>
    {!jobActive && <button className="text-button" onClick={() => onReindex(material.id)}>{failed ? '重试索引' : '重建索引'}</button>}
    <span className={`status-tag ${failed ? 'failed' : ''}`}>{statusLabel}</span>
  </div>
}
function fileKind(material: Material) { const name = material.filename ?? material.name ?? ''; return name.split('.').pop()?.toUpperCase().slice(0, 4) || 'FILE' }

function PlanView({ course, onError }: { course: Course; onError: (message: string) => void }) {
  const [plan, setPlan] = useState<Plan | null>(null)
  const [loaded, setLoaded] = useState(false)
  useEffect(() => {
    setPlan(null); setLoaded(false)
    api.plan(course.id).then(payload => { setPlan(payload.plan); setLoaded(true) }).catch(error => onError(errorText(error)))
  }, [course.id])
  return <section className="page"><div className="page-inner">
    <div className="hero"><div><p className="eyebrow">学习计划</p><h1 className="course-heading"><i style={{ backgroundColor: course.color }} />{course.name}</h1><p>在对话里说要排计划或调整计划，助手会写入这里；每次改动升一个版本，历史条目不会被改写。</p></div></div>
    {!loaded ? <p className="mini-empty">正在读取计划…</p> : plan ? <article className="card"><div className="card-heading"><div><h2>当前计划</h2><p>版本 v{plan.version} · {plan.items.length} 个条目 · 更新于 {plan.updated_at.slice(0, 16).replace('T', ' ')}</p></div></div>{plan.items.map(item => <div className="material-row" key={item.id}><div className="file-mark">{item.due_date.slice(5)}</div><div className="material-copy"><b>{item.title}</b><small>{item.status}{item.concept_name ? ` · ${item.concept_name}` : ''}</small></div></div>)}</article> : <article className="card"><h2>还没有学习计划</h2><p>在对话里告诉助手考试日期和复习范围，让它排一份计划，这里就会显示。此页只读服务端持久化状态，不展示本地虚构数据。</p></article>}
  </div></section>
}
function ArchiveView({ course, onError }: { course: Course; onError: (message: string) => void }) {
  const [archive, setArchive] = useState<ArchiveSummary | null>(null)
  useEffect(() => {
    setArchive(null)
    api.archive(course.id).then(setArchive).catch(error => onError(errorText(error)))
  }, [course.id])
  return <section className="page"><div className="page-inner">
    <div className="hero"><div><p className="eyebrow">学习档案</p><h1 className="course-heading"><i style={{ backgroundColor: course.color }} />{course.name}</h1><p>掌握度由 append-only 证据事件流投影而来；此页展示服务端已持久化的事件。</p></div></div>
    {!archive ? <p className="mini-empty">正在读取档案…</p> : <>
      <article className="card"><div className="card-heading"><div><h2>概念掌握度</h2><p>BKT 后验 × 遗忘曲线；证据不足的概念不给强弱判断</p></div></div>
        {archive.mastery.length ? archive.mastery.map(item => <div className="material-row" key={item.concept_id}>
          <div className="file-mark">{item.insufficient_evidence ? '—' : `${Math.round((item.score ?? 0) * 100)}`}</div>
          <div className="material-copy"><b>{item.name}</b>
            <small>{item.insufficient_evidence ? `数据不足（${item.objective_events} 条客观证据）` : `${item.objective_events} 条客观证据`}{item.due_at ? ` · 复习到期 ${item.due_at.slice(0, 10)}` : ''}</small>
            {!item.insufficient_evidence && <div className="job-progress"><i style={{ width: `${Math.round((item.score ?? 0) * 100)}%` }} /></div>}
          </div>
        </div>) : <div className="empty-inline">还没有掌握度数据。做练习并提交作答后，这里会按概念出现掌握度。</div>}
      </article>
      <article className="card"><div className="card-heading"><div><h2>证据事件</h2><p>共 {archive.evidence_count} 条</p></div></div>{archive.events.length ? archive.events.map(event => <div className="material-row" key={event.id}><div className="file-mark">{event.kind.toUpperCase().slice(0, 4)}</div><div className="material-copy"><b>{event.concept_name ?? event.topic_hint ?? (event.concept_id ? "已归因概念" : "未归因")}</b><small>{event.attribution_status} · {timeLabel(event.created_at)}</small></div></div>) : <div className="empty-inline">还没有证据事件。答题、小测与纠错发生后，这里会出现可追溯的记录。</div>}</article>
      {archive.unattributed.length > 0 && <article className="card"><div className="card-heading"><div><h2>未归因主题</h2><p>模型给不出概念时留下的线索，可人工补录到概念目录</p></div></div>
        {archive.unattributed.map(item => <div className="material-row" key={item.topic_hint}><div className="file-mark">{item.hits}</div><div className="material-copy"><b>{item.topic_hint}</b><small>最近 {timeLabel(item.last_seen)}</small></div></div>)}
      </article>}
    </>}
  </div></section>
}

function SettingsView({ courses, onError }: { courses: Course[]; onError: (message: string) => void }) {
  const [health, setHealth] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(false)
  async function check() { setLoading(true); try { setHealth(await api.health()) } catch (error) { onError(errorText(error)) } finally { setLoading(false) } }
  const llm = (health?.llm ?? null) as Record<string, unknown> | null
  const rag = (health?.rag ?? null) as Record<string, unknown> | null
  const embedding = (rag?.embedding ?? null) as Record<string, unknown> | null
  return <section className="page"><div className="page-inner"><div className="hero"><div><h1>管理与设置</h1><p>课程、服务能力与后续的 Skills、飞书渠道设置分开管理。</p></div><button className="ghost-button" onClick={check} disabled={loading}>检查服务</button></div><div className="settings-grid"><article className="card"><h2>课程与教材</h2><p>共 {courses.length} 门课程。课程颜色由服务端稳定返回。</p>{courses.length ? courses.map(course => <div className="settings-course" key={course.id}><i style={{ backgroundColor: course.color }} /><b>{course.name}</b><span>{course.wiki_enabled ? 'Wiki 已开启' : 'Wiki 已关闭'}</span></div>) : <p className="empty-inline">暂无课程，请从左栏创建。</p>}</article><article className="card"><h2>Skills</h2><p>Skill 上传与安装接口尚未列入 2.0 Demo API 契约。上传能力默认保持关闭，避免前端伪造安装状态。</p><button className="ghost-button" disabled>上传 Skill（等待接口）</button></article><article className="card"><h2>飞书渠道</h2><p>首版只有飞书渠道；飞书始终使用一个通用会话，不提供课程选择。密钥绝不在前端回显。</p><button className="ghost-button" disabled>配置飞书（等待接口）</button></article><article className="card health-card"><h2>运行状态</h2>{health ? <><dl>
    <div><dt>回答模型</dt><dd>{llm ? `${String(llm.provider)} / ${String(llm.model)} · ${llm.enabled ? '远端已启用' : '本地 Demo responder'}` : '未知'}</dd></div>
    <div><dt>检索方式</dt><dd>{rag?.backend === 'hybrid_bge' ? '语义 + 词面混合' : '仅词面'}</dd></div>
    {embedding && <div><dt>向量模型</dt><dd>{String(embedding.model)} · {embedding.error ? `加载失败：${String(embedding.error)}` : embedding.loaded ? '已加载' : '待首次使用时加载'}</dd></div>}
    <div><dt>数据库</dt><dd>{(health.database as Record<string, unknown>)?.ok ? `正常 · migration v${String((health.database as Record<string, unknown>)?.migration_version)}` : '异常'}</dd></div>
  </dl><details><summary>原始 JSON</summary><pre>{JSON.stringify(health, null, 2)}</pre></details></> : <p>点击“检查服务”查看模型与检索的真实状态。</p>}</article></div></div></section>
}
function CoursePickerState({ view, courses, onPick, onCreate }: { view: View; courses: Course[]; onPick: (courseId: string) => void; onCreate: () => void }) {
  return <section className="page"><div className="page-inner empty-course"><span aria-hidden>❯</span><h1>先选择一个课程</h1><p>{viewNames[view]}以课程为边界。选择后左栏也会切换到该课程工作区。</p>
    <div className="picker-grid">{courses.map(item => <button className="picker-card" key={item.id} onClick={() => onPick(item.id)}><i style={{ backgroundColor: item.color }} /><b>{item.name}</b>{item.wiki_enabled && <em>Wiki</em>}</button>)}<button className="picker-card picker-create" onClick={onCreate}>＋ 新建课程</button></div>
  </div></section>
}
function CitationDrawer({ citation, onClose }: { citation: Citation; onClose: () => void }) { return <aside className="citation-drawer" role="dialog" aria-label="教材引用详情"><header><div><p>教材引用</p><h2>{citation.material_name ?? '资料片段'}</h2></div><button aria-label="关闭引用详情" onClick={onClose}>×</button></header><p className="citation-location">{citation.page ? `第 ${citation.page} 页` : citation.chunk_id ? `片段 ${citation.chunk_id}` : '服务端返回的资料定位'}</p><blockquote>{citation.text ?? '该引用未提供可展示的原文片段。'}</blockquote>{citation.score !== undefined && <p>检索排序分：{citation.score.toFixed(4)}</p>}</aside> }
