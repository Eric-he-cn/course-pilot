import { ChangeEvent, FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
import { ApiError, api, clearCurrentUser, currentModel, currentThinking, currentUser, onConnectionLost, setCurrentModel, setCurrentThinking, setCurrentUser } from './api'
import type { ArchiveSummary, Attachment, Citation, ContextUsage, Course, Job, Material, Message, Plan, ScopeMode, SearchResult, NoteSummary, SessionSummary, SkillInfo, ToolActivity } from './types'

type View = 'chat' | 'library' | 'plan' | 'archive' | 'settings' | 'help'
type Workspace = { scope: ScopeMode; courseId?: string }
type TurnResolution = { sessionId: string; status: string; courseId: string | null; courseName: string | null }

const viewNames: Record<View, string> = { chat: '对话', library: '知识仓库', plan: '学习计划', archive: '学习档案', settings: '管理与设置', help: '使用说明' }
const nav: { id: View; num: string }[] = [
  { id: 'chat', num: '01' }, { id: 'library', num: '02' }, { id: 'plan', num: '03' }, { id: 'archive', num: '04' },
]
const MAX_MATERIAL_BYTES = 100 * 1024 * 1024
const TOOL_LABELS: Record<string, string> = {
  search_materials: '检索教材', list_materials: '资料清单', get_plan: '学习计划', plan_update: '写入计划',
  get_archive: '学习档案', concept_search: '概念目录', emit_evidence: '记录学习证据', memory_patch: '更新记忆',
  use_skill: '加载能力', artifact_read: '读取练习', artifact_append: '保存练习',
  web_search: '联网检索', web_fetch: '读取网页', note_write: '写入笔记', note_read: '读取笔记',
  calculator: '计算',
}

const TOOL_CAPABILITY_HINT: Record<string, string> = {
  search_materials: 'read_course', list_materials: 'read_course', get_plan: 'read_course',
  get_archive: 'read_course', concept_search: 'read_course', note_read: 'read_course',
  emit_evidence: 'write_state', plan_update: 'write_state', memory_patch: 'write_state',
  artifact_append: 'write_state', note_write: 'write_note',
  web_search: 'network', web_fetch: 'network',
  calculator: 'free', use_skill: 'free', artifact_read: 'free',
}

function errorText(error: unknown) { return error instanceof Error ? error.message : '发生未知错误，请重试。' }
function timeLabel(value?: string) { return value ? new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', month: 'numeric', day: 'numeric' }).format(new Date(value)) : '刚刚' }

const ADJECTIVES = ['勤奋的', '好奇的', '专注的', '沉稳的', '敏捷的', '踏实的', '爱问的', '安静的']
const CREATURES = ['水獺', '猫头鹰', '小海豹', '刺猬', '树懒', '狐狸', '柯基', '企鹅']

function randomNames(count = 5): string[] {
  const picked = new Set<string>()
  while (picked.size < count) {
    picked.add(ADJECTIVES[Math.floor(Math.random() * ADJECTIVES.length)] + CREATURES[Math.floor(Math.random() * CREATURES.length)])
  }
  return [...picked]
}

/** 登录：只输用户名，没有密码。用途是把不同人的资料分开存，不是访问控制。 */
function LoginView({ onLogin }: { onLogin: (name: string) => void }) {
  const [name, setName] = useState(() => currentUser())
  const [suggestions] = useState(() => randomNames())
  const [error, setError] = useState('')
  const remembered = currentUser()
  function submit(event: FormEvent) {
    event.preventDefault()
    const value = name.trim()
    if (!value) { setError('请输入一个用户名'); return }
    if (value.length > 32) { setError('用户名不能超过 32 个字符'); return }
    if (!/^[\p{L}\p{N} _-]+$/u.test(value)) { setError('只能用中日韩文字、字母、数字、空格与 - _'); return }
    onLogin(value)
  }
  return <div className="login-screen">
    <form className="login-card" onSubmit={submit}>
      <div className="brand"><div className="brandmark">{'>_'}</div><div className="brand-copy"><strong>CoursePilot</strong><span className="ver">v2.0</span></div></div>
      <h1>用哪个名字继续？</h1>
      <p>每个用户名对应一份独立的课程、教材与学习记录。<strong>没有密码</strong>，
        同一台机器上知道名字就能进。</p>
      <input value={name} autoFocus aria-label="用户名" placeholder="输入用户名"
        onChange={event => { setName(event.target.value); setError('') }} />
      {error && <span className="login-error">{error}</span>}
      <div className="login-suggestions">
        <span>随便挑一个：</span>
        {suggestions.map(item => <button type="button" key={item} onClick={() => { setName(item); setError('') }}>{item}</button>)}
      </div>
      <button className="login-submit" type="submit">{remembered && remembered === name.trim() ? `以「${remembered}」继续` : '进入'}</button>
      {remembered && <small>上次用的是「{remembered}」</small>}
    </form>
  </div>
}

export default function App() {
  const [username, setUsername] = useState(() => currentUser())
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
  // 帮助页点例句后带进对话输入框
  const [draftSeed, setDraftSeed] = useState('')
  // 停止生成：中断 SSE 读取，服务端 finally 会把这一轮落成终态，已生成的内容仍在库里
  const abortRef = useRef<AbortController | null>(null)
  // reader.cancel() 会让读取正常结束而不抛错，所以"是否被停止"要显式记，不能靠捕获异常判断。
  const stoppedRef = useRef(false)

  const course = useMemo(() => courses.find(item => item.id === workspace.courseId) ?? null, [courses, workspace.courseId])
  const heading = activeSession?.title && view === 'chat' ? activeSession.title : viewNames[view]

  useEffect(() => { localStorage.setItem('cp-sidebar-collapsed', String(sidebarCollapsed)) }, [sidebarCollapsed])
  const heartbeat = useCallback(async () => {
    try {
      const payload = await api.health()
      // 后端真的在，才算在线：代理给的 500 走不到这里。
      setHealth(payload); setApiOnline(true)
    } catch {
      setApiOnline(false)
    }
  }, [])
  useEffect(() => {
    onConnectionLost(() => setApiOnline(false))
    void heartbeat()
    api.courses().then(setCourses).catch(error => setNotice(errorText(error)))
  }, [])
  // health 是唯一权威的探针：只靠用户操作发现不了掉线（盯着状态栏不点东西时没有请求），
  // 而开发时走 vite 代理，后端挂了拿到的是代理给的 500，不能当成在线。
  useEffect(() => {
    const timer = window.setInterval(heartbeat, apiOnline === false ? 5000 : 15000)
    return () => window.clearInterval(timer)
  }, [apiOnline, heartbeat])
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
  // keepView：从某个页面内选课程时留在当前页，不要弹回对话。
  function switchWorkspace(next: Workspace, options: { keepView?: boolean } = {}) {
    setWorkspace(next)
    if (!options.keepView) setView('chat')
    setSidebarOpen(false); setCitation(null); setTurnResolution(null); setContextUsage(null)
  }
  async function newSession() {
    setBusy(true)
    try {
      const session = await api.createSession(workspace.scope, workspace.courseId)
      setSessions(current => [session, ...current]); setActiveSession(session); setView('chat'); setMessages([])
    } catch (error) { setNotice(errorText(error)) } finally { setBusy(false) }
  }
  async function renameSession(title: string, sessionId?: string) {
    const target = sessionId ?? activeSession?.id
    if (!target) return
    try {
      const updated = await api.renameSession(target, title)
      setSessions(current => current.map(item => item.id === updated.id ? updated : item))
      if (activeSession?.id === updated.id) setActiveSession(updated)
    } catch (error) { setNotice(errorText(error)) }
  }
  function courseDeleted(courseId: string) {
    setCourses(current => current.filter(item => item.id !== courseId))
    // 当前正停在这门课的工作区就退回通用模式，否则界面还挂在一门不存在的课上。
    if (workspace.courseId === courseId) switchWorkspace({ scope: 'general' })
    void loadSessions()
  }
  async function deleteSession(session: SessionSummary) {
    try {
      await api.deleteSession(session.id)
      setSessions(current => current.filter(item => item.id !== session.id))
      // 删的是当前打开的那个就退回空白，否则界面还停在已经不存在的会话上。
      if (activeSession?.id === session.id) { setActiveSession(null); setMessages([]) }
    } catch (error) { setNotice(errorText(error)) }
  }
  async function createCourse() {
    const name = window.prompt('课程名称')?.trim(); if (!name) return
    setBusy(true)
    try { const created = await api.createCourse(name); setCourses(current => [...current, created]); switchWorkspace({ scope: 'course', courseId: created.id }) }
    catch (error) { setNotice(errorText(error)) } finally { setBusy(false) }
  }

  if (!username) return <LoginView onLogin={name => { setCurrentUser(name); setUsername(name); window.location.reload() }} />

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
        <span className="workspace-copy"><b>通用模式</b><small>按问题自动判断课程</small></span>
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
      <div className="sessions-head"><span>SESSIONS</span></div>
      <div className="session-list">
        {sessions.length ? sessions.map(session => <SessionRow key={session.id} session={session}
          active={session.id === activeSession?.id}
          onOpen={() => { setActiveSession(session); setView('chat'); setSidebarOpen(false) }}
          onRename={async title => { await renameSession(title, session.id) }}
          onDelete={async () => { await deleteSession(session) }} />) : <p className="mini-empty">此工作区还没有会话。</p>}
      </div>
      <button className="new-session" onClick={newSession} disabled={busy}>＋ 新建{workspace.scope === 'general' ? '通用' : '课程'}会话</button>
      <div className="sidebar-foot">
        <button onClick={() => { setView('help'); setSidebarOpen(false) }}>? <span>使用说明</span></button>
        <button onClick={() => { clearCurrentUser(); window.location.reload() }} title={`当前：${username}`}>⇄ <span>切换用户（{username}）</span></button>
        <button onClick={() => { setView('settings'); setSidebarOpen(false) }}>⚙ <span>管理与设置</span></button>
      </div>
    </aside>
    <main className="main">
      <header className="topbar">
        <button className="icon-button mobile-only" aria-label="打开导航" onClick={() => setSidebarOpen(true)}>☰</button>
        <button className="icon-button collapse-only" aria-label="折叠侧栏" onClick={() => setSidebarCollapsed(value => !value)}>☷</button>
        <div className="title-area">
          {view === 'chat' && activeSession
            ? <SessionTitle session={activeSession} onRename={renameSession} />
            : <b>{heading}</b>}
          <span className="crumb"><i style={{ backgroundColor: course?.color ?? '#D4D4D8' }} /> {workspaceName}</span>
        </div>
      </header>
      {notice && <div className="notice" role="alert"><span>{notice}</span><button aria-label="关闭错误提示" onClick={() => setNotice('')}>×</button></div>}
      {view === 'chat' && <ChatView session={activeSession} messages={messages} workspaceName={workspaceName} scope={workspace.scope} turnResolution={turnResolution} contextUsage={contextUsage} draftSeed={draftSeed} onSeedUsed={() => setDraftSeed('')} onCitation={setCitation} onUpload={async file => {
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
        setMessages(current => [...current, optimistic, { id: pendingId, role: 'assistant', content: '', status: 'streaming' }]); setBusy(true)
        const controller = new AbortController()
        abortRef.current = controller
        stoppedRef.current = false
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
            setContextUsage({ segments: payload.segments, total_chars: payload.total_chars ?? 0, limit_chars: payload.limit_chars ?? 1, history_budget_chars: payload.history_budget_chars ?? 0, dropped_history: payload.dropped_history ?? 0, clipped_history: payload.clipped_history ?? 0, compacted_messages: payload.compacted_messages ?? 0 })
          }
          if (payload.type === 'tool_call' && payload.call_id) {
            activity.push({ call_id: payload.call_id, name: payload.name ?? '工具', origin: payload.origin, started_at: Date.now() })
            setMessages(current => current.map(item => item.id === pendingId ? { ...item, activity: [...activity] } : item))
          }
          if (payload.type === 'tool_result' && payload.call_id) {
            const entry = activity.find(item => item.call_id === payload.call_id)
            if (entry) { entry.summary = payload.summary; entry.ok = payload.ok; entry.elapsed_ms = entry.started_at ? Date.now() - entry.started_at : undefined }
            setMessages(current => current.map(item => item.id === pendingId ? { ...item, activity: [...activity] } : item))
          }
          if (payload.type === 'text_delta' && payload.text) {
            const delta = payload.text
            setMessages(current => current.map(item => item.id === pendingId ? { ...item, content: item.content + delta } : item))
          }
          if (payload.type === 'provider_fallback') {
            // 远端模型不可用时会静默切到本地兜底（无工具、无检索）。不上屏的话，
            // 用户会把质量完全不同的回答当成正常回答。
            setMessages(current => current.map(item => item.id === pendingId ? { ...item, degraded: `远端模型 ${payload.provider ?? ''} 不可用，本次已切换到本地兜底模型` } : item))
          }
          if (payload.type === 'turn_completed' && payload.finish_reason === 'length') setNotice('回答达到长度上限，内容可能不完整。')
        }, attachmentIds, controller.signal)
          if (stoppedRef.current) {
            // 客户端断连时服务端生成器可能挂在 yield 上不进 finally，部分回答不一定落盘，
            // 所以保留本地已渲染的内容并标明它没有保存。
            setMessages(current => current.map(item => item.id === pendingId
              ? { ...item, status: 'stopped', content: item.content || '（已停止，这一轮没有生成内容）' }
              : item))
            await loadSessions()
          } else {
            await loadMessages(targetSession.id); await loadSessions()
          }
        }
        catch (error) {
          if (stoppedRef.current) {
            setMessages(current => current.map(item => item.id === pendingId
              ? { ...item, status: 'stopped', content: item.content || '（已停止，这一轮没有生成内容）' }
              : item))
            void loadSessions()
            return
          }
          setNotice(errorText(error))
          // 优先回读服务端真值（部分回答已带 interrupted 状态持久化）；服务不可达时保留本地标记。
          try { await loadMessages(targetSession.id); await loadSessions() }
          catch { setMessages(current => current.map(item => item.id === pendingId ? { ...item, content: item.content || '回答中断了，重发一次。', artifact: { kind: 'interrupted' } } : item)) }
        }
        finally { setBusy(false); abortRef.current = null }
      }} busy={busy} onStop={() => { stoppedRef.current = true; abortRef.current?.abort() }} />}
      {!['chat', 'settings', 'help'].includes(view) && !course && <CoursePickerState view={view} courses={courses} onPick={courseId => switchWorkspace({ scope: 'course', courseId }, { keepView: true })} onCreate={createCourse} />}
      {view === 'library' && course && <LibraryView course={course} onCourseChange={updated => setCourses(current => current.map(item => item.id === updated.id ? updated : item))} onError={setNotice} />}
      {view === 'plan' && course && <PlanView course={course} onError={setNotice} />}
      {view === 'archive' && course && <ArchiveView course={course} onError={setNotice} />}
      {view === 'settings' && <SettingsView courses={courses} onError={setNotice} onCourseDeleted={courseDeleted} />}
      {view === 'help' && <HelpView courses={courses} health={health} onError={setNotice} onTry={text => { setView('chat'); setDraftSeed(text) }} />}
      <footer className="statusbar">
        <span className={apiOnline ? 'ok' : 'bad'}>● {apiOnline ? 'connected' : 'offline'}</span>
        {/* 掉线时这些都是缓存的旧值，留着会让人以为服务还在 */}
        {apiOnline !== false && healthLlm && <ModelPicker llm={healthLlm} />}
        {apiOnline !== false && healthRag && <span className="statusbar-detail">{String(healthRag.backend).includes('bge') ? '混合检索' : '关键词检索'}</span>}
        {apiOnline !== false && view === 'chat' && <span className="statusbar-detail">回答优先用当前课程的资料，没命中教材会标注出来</span>}
        <span className="right">CoursePilot v2.0</span>
      </footer>
    </main>
    {citation && <CitationDrawer citation={citation} onClose={() => setCitation(null)} />}
  </div>
}

function ChatView({ session, messages, workspaceName, scope, turnResolution, contextUsage, draftSeed, onSeedUsed, onCitation, onUpload, onSend, onStop, busy }: { session: SessionSummary | null; messages: Message[]; workspaceName: string; scope: ScopeMode; turnResolution: TurnResolution | null; contextUsage: ContextUsage | null; draftSeed: string; onSeedUsed: () => void; onStop: () => void; onCitation: (citation: Citation) => void; onUpload: (file: File) => Promise<Attachment>; onSend: (content: string, attachmentIds: string[]) => Promise<void>; busy: boolean }) {
  const [draft, setDraft] = useState(''); const composer = useRef<HTMLTextAreaElement>(null)
  useEffect(() => { if (draftSeed) { setDraft(draftSeed); onSeedUsed(); composer.current?.focus() } }, [draftSeed])
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
  const scroller = useRef<HTMLDivElement>(null)
  const lastContent = messages.length ? messages[messages.length - 1].content.length : 0
  // 换会话就贴到最新一条：不控制的话滚动位置由渲染时序决定，同一个会话两次进去可能停在不同地方。
  useEffect(() => {
    const box = scroller.current
    if (box) box.scrollTop = box.scrollHeight
  }, [session?.id, messages.length])
  // 流式追加时跟随，但用户手动往上翻了就别把他拽回来。
  useEffect(() => {
    const box = scroller.current
    if (!box) return
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120
    if (nearBottom) box.scrollTop = box.scrollHeight
  }, [lastContent])

  const contextNote = !session ? '发送第一条消息会自动创建会话。'
    : session.scope_mode !== 'general' ? ''
    : turnResolution?.sessionId === session.id
      ? (turnResolution.status === 'resolved'
          ? `本轮解析到：${turnResolution.courseName ?? turnResolution.courseId}`
          : '本轮未解析到课程 · 在问题里说明课程名即可')
      : session.resolved_course_id ? `最近解析到：${session.course_name ?? session.resolved_course_id}` : ''
  return <section className="chat-view">
    {/* 课程会话的会话名与课程顶栏已经显示了，这里只留通用会话才有的逐轮解析结果。 */}
    {contextNote && <div className="session-context">{contextNote}</div>}
    <div className="messages" aria-live="polite" ref={scroller}>
      {!messages.length && <div className="welcome"><span aria-hidden>❯</span><h1>今天想从哪里开始？</h1><p>{isCourseScope ? `这里的提问固定使用「${workspaceName}」的资料，回答会带教材页码引用。` : '通用模式每轮按问题解析课程。直接说出课程名最准。'}</p><div className="suggestion-row">{(isCourseScope ? ['讲讲这门课的核心概念', '给我出几道练习题', '帮我制定复习计划'] : ['「课程名」的某个概念怎么理解？', '给我出几道练习题', '帮我制定复习计划']).map(text => <button key={text} className="suggestion-chip" onClick={() => { setDraft(text); composer.current?.focus() }}>{text}</button>)}</div></div>}
      {messages.filter(item => item.role !== 'system').map((message, index, list) => <MessageCard message={message} key={message.id} onCitation={onCitation} showResolution={!isCourseScope}
        onRetry={busy ? undefined : (() => {
          // 重发这条回答对应的那句提问——往前找最近的一条 user 消息
          const asked = [...list.slice(0, index)].reverse().find(item => item.role === 'user')
          return asked ? () => void onSend(asked.content, []) : undefined
        })()} />)}
    </div>
    <form className="composer-wrap" onSubmit={submit}>
      {(attachments.length > 0 || uploading) && <div className="attach-list">
        {attachments.map(item => <div className={item.needs_confirmation ? 'attach-chip warn' : 'attach-chip'} key={item.id}>
          <span className="attach-name">IMG · {item.filename}</span>
          <span className="attach-preview">{item.needs_confirmation ? '没识别出文字，发送前在消息里补充说明' : item.transcription}</span>
          <button type="button" aria-label={`移除图片 ${item.filename}`} onClick={() => setAttachments(current => current.filter(other => other.id !== item.id))}>×</button>
        </div>)}
        {uploading && <div className="attach-chip pending"><span className="attach-name">IMG</span><span className="attach-preview">正在转录图片文字…</span></div>}
      </div>}
      <div className="composer"><span className="prompt" aria-hidden>❯</span><textarea ref={composer} value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit() } }} placeholder={session ? '写下你的思路，或继续提问…' : '先新建一个会话…'} aria-label="输入消息" rows={2} /><div className="composer-row"><button type="button" className="attach-button" onClick={() => fileInput.current?.click()} disabled={busy || uploading} aria-label="上传图片提问"><svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden><rect x="1.5" y="2.5" width="13" height="11" rx="1.5" /><circle cx="5.5" cy="6.5" r="1.2" /><path d="M2.5 12.5 6.5 9l3 2.5 2-1.5 2 2" /></svg>图片</button><span>Enter 发送 · Shift+Enter 换行</span>{contextUsage && <ContextMeter usage={contextUsage} />}{busy
      ? <button className="send-button stop" type="button" onClick={onStop} aria-label="停止生成" title="停止生成">■</button>
      : <button className="send-button" type="submit" disabled={!draft.trim() || uploading} aria-label="发送消息">↑</button>}</div></div>
      <input ref={fileInput} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={pickFile} />
    </form>
  </section>
}

/** mermaid 代码块渲染成图；流式期间代码往往不完整，渲染失败就保留代码本身。 */
function Mermaid({ code }: { code: string }) {
  const [svg, setSvg] = useState('')
  const [failed, setFailed] = useState(false)
  const id = useRef(`mermaid-${Math.random().toString(36).slice(2)}`)
  useEffect(() => {
    let cancelled = false
    // 流式期间每个增量都会改 code。不防抖就会每秒渲染几十次，而且代码没写完时
    // 渲染失败又退回源码，画面在图与源码之间来回跳。等它稳定下来再渲染一次。
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const mermaid = (await import('mermaid')).default
          mermaid.initialize({ startOnLoad: false, theme: 'neutral', securityLevel: 'strict' })
          const { svg: rendered } = await mermaid.render(id.current, code)
          if (!cancelled) { setSvg(rendered); setFailed(false) }
        } catch {
          // 已经画出过就保留上一版，别把成图退回源码。
          if (!cancelled) setFailed(current => current || !svg)
        }
      })()
    }, 400)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [code])

  function download() {
    const blob = new Blob([svg], { type: 'image/svg+xml;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `coursepilot-图示-${new Date().toISOString().slice(0, 10)}.svg`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  if (!svg) return <pre className={failed ? 'mermaid-source failed' : 'mermaid-source'}>
    <span className="mermaid-hint">{failed ? '这段图示代码有语法问题，下面是原文' : '正在生成图示…'}</span>
    <code>{code}</code>
  </pre>
  return <figure className="mermaid-figure">
    <div role="img" aria-label="图示" dangerouslySetInnerHTML={{ __html: svg }} />
    <figcaption>
      <button type="button" onClick={download}>下载 SVG</button>
      <span>可用浏览器或任意矢量图工具打开</span>
    </figcaption>
  </figure>
}

const markdownComponents = {
  code(props: { className?: string; children?: unknown }) {
    const code = String(props.children ?? '').replace(/\n$/, '')
    if (props.className?.includes('language-mermaid')) return <Mermaid code={code} />
    return <code className={props.className}>{props.children as never}</code>
  },
  // 宽表格会横向撑破对话列，套一层滚动容器。
  table(props: { children?: unknown }) {
    return <div className="table-scroll"><table>{props.children as never}</table></div>
  },
}


const CAPABILITY_GROUPS: { key: string; label: string; hint: string }[] = [
  { key: 'read_course', label: '只读你的课程数据', hint: '检索教材、查概念目录、读计划与档案、读笔记' },
  { key: 'write_state', label: '会改学习状态', hint: '写证据事件、改学习计划、更新长期记忆' },
  { key: 'write_note', label: '会新建课程笔记', hint: '把整理好的内容写进 data/notes/' },
  { key: 'network', label: '会访问外部网络', hint: '联网检索与抓取网页，每轮有次数上限' },
  { key: 'free', label: '无副作用', hint: '算术求值、加载能力、读跨轮产物' },
]

/** 使用说明。可数的内容一律来自接口，避免变成需要人工同步的死文档。 */
function HelpView({ courses, health, onError, onTry }: { courses: Course[]; health: Record<string, unknown> | null; onError: (message: string) => void; onTry: (text: string) => void }) {
  const [skills, setSkills] = useState<SkillInfo[] | null>(null)
  const [indexedCourses, setIndexedCourses] = useState<string[]>([])
  const [hasSession, setHasSession] = useState(false)
  useEffect(() => {
    api.skills().then(payload => setSkills(payload.skills)).catch(error => { setSkills([]); onError(errorText(error)) })
    void (async () => {
      const indexed: string[] = []
      for (const course of courses) {
        try {
          const materials = await api.materials(course.id)
          if (materials.some(item => (item.index_status ?? item.status) === 'indexed')) indexed.push(course.name)
        } catch { /* 单门课读不到不影响清单 */ }
      }
      setIndexedCourses(indexed)
      try { setHasSession((await api.sessions('course')).length + (await api.sessions('general')).length > 0) } catch { /* 同上 */ }
    })()
  }, [courses.length])

  const rag = (health?.rag ?? null) as Record<string, unknown> | null
  const llm = (health?.llm ?? null) as Record<string, unknown> | null
  const web = (health?.web ?? null) as Record<string, unknown> | null
  const steps = [
    { done: courses.length > 0, title: '新建一门课程', hint: '左栏「＋ 新建课程」' },
    { done: indexedCourses.length > 0, title: '上传教材并等索引完成', hint: '知识仓库页上传 PDF / TXT / MD，单个 ≤ 100 MiB' },
    { done: hasSession, title: '开始提问', hint: '回答会带教材文件名与页码，可点开看原文' },
  ]
  const grouped = CAPABILITY_GROUPS.map(group => ({
    ...group,
    tools: Object.entries(TOOL_LABELS).filter(([name]) => TOOL_CAPABILITY_HINT[name] === group.key).map(([, label]) => label),
  })).filter(group => group.tools.length > 0)

  return <section className="page"><div className="page-inner">
    <div className="hero"><div><p className="eyebrow">使用说明</p><h1>CoursePilot 能做什么</h1>
      <p>本页的清单与能力来自当前实例的实时状态。</p></div></div>

    <article className="card"><h2>上手四步</h2>
      <p>已完成的会自动打勾，依据是库里的真实数据。</p>
      {steps.map((step, index) => <div className={`help-step ${step.done ? 'done' : ''}`} key={step.title}>
        <i aria-hidden>{step.done ? '✓' : index + 1}</i>
        <div><b>{step.title}</b><small>{step.hint}</small></div>
      </div>)}
    </article>

    <article className="card"><h2>两种会话模式</h2>
      <div className="help-columns">
        <div><b>通用会话</b><p>每轮按你的问题解析课程。指向不止一门课时会<strong>先问你</strong>，不跨课程取证。
          课程名互相包含时（「深度学习」与「深度学习进阶」）取更具体的那个；问题里没有课程名时，用模型判一次学科。</p></div>
        <div><b>课程会话</b><p>固定一门课，所有提问都只用这门课的资料。适合连续学一章内容。</p></div>
      </div>
    </article>

    <article className="card"><h2>专项能力{skills ? ` · ${skills.length} 个` : ''}</h2>
      <p>说出对应的话就会自动加载相应规程，不需要手动选。点例句可以直接试。</p>
      {skills === null ? <p className="mini-empty">正在读取…</p> : skills.filter(item => item.status === 'enabled').map(skill => <div className="help-skill" key={skill.name}>
        <div className="help-skill-head"><b>{skill.name}</b><span>{skill.origin === 'builtin' ? '内建' : '导入'}</span></div>
        <p>{skill.description}</p>
        <small>什么时候用：{skill.when_to_use}</small>
        {skill.examples && skill.examples.length > 0 && <div className="help-examples">
          {skill.examples.map(example => <button type="button" key={example} onClick={() => onTry(example)}>{example}</button>)}
        </div>}
      </div>)}
    </article>

    <article className="card"><h2>当前实例状态</h2>
      <dl className="help-facts">
        <div><dt>回答模型</dt><dd>{llm ? `${String(llm.provider)} / ${String(llm.model)}${llm.enabled ? '' : '（远端未启用，走本地兜底，回答不带教材检索）'}` : '未知'}</dd></div>
        <div><dt>教材检索</dt><dd>{rag?.backend === 'hybrid_bge' ? '语义 + 词面混合' : '仅词面。中文问题命中英文教材效果差，可在知识仓库点一次「重建索引」'}</dd></div>
        <div><dt>联网</dt><dd>{web && (web as Record<string, unknown>).enabled ? '已启用。每轮最多检索 5 次、抓取 5 次；同一个查询重复调用不占额度' : '未启用（缺 RESEARCH_SERPAPI_API_KEY 或未开远端调用）'}</dd></div>
        <div><dt>硬限制</dt><dd>单个教材 ≤ 100 MiB，对话图片 ≤ 10 MiB，一轮最多 10 次工具调用（加载能力后 16 次）</dd></div>
      </dl>
    </article>

    <article className="card"><h2>它能碰到什么</h2>
      <p>工具按副作用分组，后三组会改数据或出网。</p>
      {grouped.map(group => <div className="help-group" key={group.key}>
        <div><b>{group.label}</b><small>{group.hint}</small></div>
        <span>{group.tools.join('、')}</span>
      </div>)}
      <p className="help-note">导入的第三方 skill 拿不到计划、记忆、笔记与联网。
        权限取「声明 ∩ 白名单」，越权申请直接拒绝。</p>
    </article>

    <article className="card"><h2>不做什么</h2>
      <p>播客音频、通用闪卡产品、泛化每日简报、整卷模拟考试、社交对战、多租户商业化。
        通用会话不在同一轮里跨多门课读写；解析不出唯一课程时会先问你。</p>
    </article>
  </div></section>
}

function CourseSettingRow({ course, onDelete, onError }: {
  course: Course; onDelete: (courseId: string) => void; onError: (message: string) => void
}) {
  const [confirming, setConfirming] = useState(false)
  return <div className="settings-course">
    <i style={{ backgroundColor: course.color }} /><b>{course.name}</b>
    <span>{course.wiki_enabled ? 'Wiki 已开启' : 'Wiki 已关闭'}</span>
    <button className="text-button danger-text" onClick={() => setConfirming(true)}>删除</button>
    {confirming && <DangerConfirm
      what={`课程「${course.name}」`}
      consequences={[
        '这门课的全部教材、切块与索引',
        '概念目录、掌握度与答题记录',
        '学习计划及其改动历史',
        '课程笔记、Wiki 页面与这门课的长期记忆',
        '属于这门课的会话；不指定课程的会话会保留',
      ]}
      onConfirm={async () => {
        setConfirming(false)
        try { await api.deleteCourse(course.id); onDelete(course.id) }
        catch (error) { onError(errorText(error)) }
      }}
      onCancel={() => setConfirming(false)} />}
  </div>
}

function DangerConfirm({ what, consequences, onConfirm, onCancel }: {
  what: string; consequences: string[]; onConfirm: () => void; onCancel: () => void
}) {
  return <div className="danger-confirm">
    <b>删除{what}？</b>
    <ul>{consequences.map(line => <li key={line}>{line}</li>)}</ul>
    <div className="danger-actions">
      <button className="danger" onClick={onConfirm}>确认删除</button>
      <button onClick={onCancel}>取消</button>
    </div>
  </div>
}

function SessionRow({ session, active, onOpen, onRename, onDelete }: {
  session: SessionSummary; active: boolean
  onOpen: () => void; onRename: (title: string) => Promise<void>; onDelete: () => Promise<void>
}) {
  const [mode, setMode] = useState<'idle' | 'rename' | 'confirm'>('idle')
  const [draft, setDraft] = useState(session.title)
  useEffect(() => { setDraft(session.title); setMode('idle') }, [session.id, session.title])

  if (mode === 'rename') return <div className="session-row editing">
    <input className="session-rename" value={draft} autoFocus aria-label="会话标题"
      onChange={event => setDraft(event.target.value)}
      onBlur={() => setMode('idle')}
      onKeyDown={event => {
        if (event.key === 'Escape') setMode('idle')
        if (event.key === 'Enter') {
          const next = draft.trim()
          setMode('idle')
          if (next && next !== session.title) void onRename(next)
        }
      }} />
  </div>

  if (mode === 'confirm') return <div className="session-row confirming">
    <span>删除这个会话？</span>
    <button className="danger" onClick={() => { setMode('idle'); void onDelete() }}>删除</button>
    <button onClick={() => setMode('idle')}>取消</button>
  </div>

  return <div className={`session-row ${active ? 'active' : ''}`}>
    <button className="session" onClick={onOpen}>
      <i title={session.scope_mode === 'general' ? '通用会话' : '课程会话'} style={{ backgroundColor: session.course_color ?? '#D4D4D8' }} /><span className="session-text"><b>{session.title || '未命名会话'}</b><small>{timeLabel(session.updated_at)}</small></span>
    </button>
    <span className="session-actions">
      <button aria-label="重命名会话" title="重命名" onClick={() => setMode('rename')}>✎</button>
      <button aria-label="删除会话" title="删除" onClick={() => setMode('confirm')}>×</button>
    </span>
  </div>
}

type ModelOption = { key: string; label: string; model: string; thinking_default: string }

// 档位名与后端 THINKING_TIERS 对应。深度只在「开」这一档有意义；
// adaptive 是让模型自己决定这一轮要不要想。
const EFFORT_TIERS = ['high', 'max']

function ModelPicker({ llm }: { llm: Record<string, unknown> }) {
  const options = (llm.choices as ModelOption[] | undefined) ?? []
  const [model, setModel] = useState(() => currentModel())
  const [tier, setTier] = useState(() => currentThinking())
  if (!llm.enabled) return <span className="statusbar-detail">{String(llm.provider)}/{String(llm.model)} · local demo</span>
  if (options.length === 0) return <span className="statusbar-detail">{String(llm.provider)}/{String(llm.model)}</span>

  const active = options.find(item => item.key === model) ?? options.find(item => item.key === llm.default_choice) ?? options[0]
  const current = tier || active.thinking_default || 'off'
  const mode = EFFORT_TIERS.includes(current) ? 'on' : current
  const effort = EFFORT_TIERS.includes(current) ? current : 'high'
  const apply = (next: string) => { setCurrentThinking(next); setTier(next) }
  return <>
    <label className="statusbar-picker">
      <span className="sr-only">对话模型</span>
      <select value={active.key} onChange={event => { setCurrentModel(event.target.value); setModel(event.target.value) }}>
        {options.map(item => <option key={item.key} value={item.key}>{item.label} · {item.model}</option>)}
      </select>
    </label>
    <label className="statusbar-picker">
      <span className="sr-only">思考模式</span>
      <select value={mode} onChange={event => apply(event.target.value === 'on' ? effort : event.target.value)}>
        <option value="off">思考 关</option>
        <option value="adaptive">思考 自动</option>
        <option value="on">思考 开</option>
      </select>
    </label>
    <label className="statusbar-picker">
      <span className="sr-only">思考深度</span>
      <select value={effort} disabled={mode !== 'on'} onChange={event => apply(event.target.value)}>
        <option value="high">深度 high</option>
        <option value="max">深度 max</option>
      </select>
    </label>
  </>
}

function ThinkingHint({ activity }: { activity?: ToolActivity[] }) {
  // 工具跑完到第一个字之间有一段空档，这里不说话用户就以为卡在上一个工具上。
  const running = activity?.find(entry => !entry.summary)
  const label = running ? `正在${TOOL_LABELS[running.name] ?? running.name}` : activity?.length ? '正在思考' : '正在准备'
  return <span className="typing">{label}<i aria-hidden /><i aria-hidden /><i aria-hidden /></span>
}

function ToolChip({ entry }: { entry: ToolActivity }) {
  const pending = !entry.summary
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!pending || !entry.started_at) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [pending, entry.started_at])
  // 耗时不是装饰：reduced-motion 下呼吸动效会被关掉，这是唯一还能说明"在动"的线索。
  const seconds = pending
    ? (entry.started_at ? Math.floor((now - entry.started_at) / 1000) : 0)
    : Math.round((entry.elapsed_ms ?? 0) / 100) / 10
  const timing = pending ? (seconds >= 2 ? ` · ${seconds}s` : '') : (entry.elapsed_ms && entry.elapsed_ms >= 1000 ? ` · ${seconds}s` : '')
  return <span className={`tool-chip ${entry.ok === false ? 'warn' : ''} ${pending ? 'pending' : 'done'}`}>
    <i aria-hidden>{pending ? '⋯' : entry.ok === false ? '×' : '✓'}</i>
    <span className="sr-only">{pending ? '进行中：' : entry.ok === false ? '失败：' : '完成：'}</span>
    {TOOL_LABELS[entry.name] ?? entry.name}{entry.summary ? ` · ${entry.summary}` : ''}{timing}
  </span>
}

/** 课程笔记：助手用 note_write 落盘的学习卡片与整理稿，此前没有任何查看入口。 */
function NotesPanel({ course, onError }: { course: Course; onError: (message: string) => void }) {
  const [notes, setNotes] = useState<NoteSummary[] | null>(null)
  const [open, setOpen] = useState<{ title: string; content: string } | null>(null)
  useEffect(() => {
    setNotes(null); setOpen(null)
    api.notes(course.id).then(payload => setNotes(payload.notes)).catch(error => { setNotes([]); onError(errorText(error)) })
  }, [course.id])
  async function read(title: string) {
    try { setOpen(await api.note(course.id, title)) } catch (error) { onError(errorText(error)) }
  }
  return <article className="card">
    <div className="card-heading"><div><h2>课程笔记</h2>
      <p>助手整理并存下的内容，落在 <code>data/notes/{course.id.slice(0, 14)}…/</code>。说「做成学习卡片」「存下来」，就会写到这里。</p></div></div>
    {notes === null ? <p className="mini-empty">正在读取…</p> : notes.length === 0
      ? <div className="empty-inline"><b>还没有笔记</b><p>让助手把内容整理成学习卡片或概念梳理，它会存到这里。</p></div>
      : notes.map(note => <div className="material-row" key={note.title}>
          <div className="file-mark">MD</div>
          <div className="material-copy"><b>{note.title}</b><small>{note.chars} 字 · 更新于 {note.updated_at.slice(0, 16).replace('T', ' ')}</small></div>
          <button className="ghost-button" onClick={() => void read(note.title)}>查看</button>
        </div>)}
    {open && <div className="note-viewer">
      <div className="note-viewer-head"><b>{open.title}</b><button onClick={() => setOpen(null)} aria-label="关闭笔记">×</button></div>
      <div className="message-content"><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={markdownComponents}>{open.content}</ReactMarkdown></div>
    </div>}
  </article>
}

function RetryCard({ title, message, onRetry }: { title: string; message: string; onRetry: () => void }) {
  return <article className="card"><h2>{title}</h2><p>{message}</p>
    <button className="ghost-button" onClick={onRetry}>重新读取</button>
  </article>
}

function SessionTitle({ session, onRename }: { session: SessionSummary; onRename: (title: string) => Promise<void> }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(session.title)
  useEffect(() => { setDraft(session.title); setEditing(false) }, [session.id, session.title])
  async function commit() {
    setEditing(false)
    const next = draft.trim()
    if (next && next !== session.title) await onRename(next)
    else setDraft(session.title)
  }
  if (editing) return <input
    className="title-input" value={draft} autoFocus aria-label="会话标题"
    onChange={event => setDraft(event.target.value)}
    onBlur={() => void commit()}
    onKeyDown={event => {
      if (event.key === 'Enter') { event.preventDefault(); void commit() }
      if (event.key === 'Escape') { setDraft(session.title); setEditing(false) }
    }} />
  return <button type="button" className="title-edit" onClick={() => setEditing(true)} title="点击重命名会话">
    <b>{session.title || '未命名会话'}</b>
    <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden><path d="M11.5 2.5l2 2-7.5 7.5-2.5.5.5-2.5z" /></svg>
  </button>
}

function ContextMeter({ usage }: { usage: ContextUsage }) {
  const [open, setOpen] = useState(false)
  const k = (chars: number) => chars >= 1000 ? `${(chars / 1000).toFixed(1)}K` : String(chars)
  const percent = Math.min(100, Math.round((usage.total_chars / usage.limit_chars) * 100))
  const filled = Math.max(1, Math.round(percent / 12.5))
  const notice = usage.dropped_history > 0 || usage.clipped_history > 0
  return <div className="context-chip">
    <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} aria-label="查看本轮上下文构成" className={notice ? 'warn' : undefined}>
      <span aria-hidden>{'▓'.repeat(filled)}{'░'.repeat(8 - filled)}</span>
      <b>{percent}%</b>
    </button>
    {open && <div className="context-popover">
      <div className="popover-head"><b>本轮上下文</b><span>{k(usage.total_chars)} / {k(usage.limit_chars)}</span></div>
      {usage.segments.map(segment => <div className="popover-row" key={segment.label}><span>{segment.label}</span><b>{k(segment.chars)}</b></div>)}
      <p>按字符数估算，实际占用通常更小。</p>
      {usage.compacted_messages > 0 && <p className="popover-note">更早的 {usage.compacted_messages} 条消息压成了摘要，仍在上下文里。</p>}
      {usage.dropped_history > 0 && <p className="popover-warn">更早的 {usage.dropped_history} 条消息未进入本轮上下文。</p>}
      {usage.clipped_history > 0 && <p className="popover-warn">有 {usage.clipped_history} 条超长消息被截断后才进入上下文。</p>}
    </div>}
  </div>
}

function PlanGantt({ items }: { items: Plan['items'] }) {
  const today = new Date().toLocaleDateString('sv')
  const sorted = [...items].sort((a, b) => a.due_date.localeCompare(b.due_date))
  const start = sorted.length ? Date.parse(sorted[0].due_date) : 0
  const rows: string[] = []
  let week = -1
  for (const item of sorted) {
    // 三十多条一路铺下来读不动，按周切段
    const index = Math.floor((Date.parse(item.due_date) - start) / 604800000)
    if (index !== week) { week = index; rows.push(`    section 第 ${index + 1} 周`) }
    // mermaid 用冒号和逗号分隔字段，而计划标题里这两样都很常见——不换掉整张图都画不出来。
    const label = item.title.replace(/[:：,，#;]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 16) || '待办'
    const state = item.status === 'done' ? 'done'
      : item.due_date === today ? 'active'
      : item.due_date < today ? 'crit' : ''
    rows.push(`    ${label} :${state}${state ? ', ' : ''}${item.due_date}, 1d`)
  }
  if (!rows.length) return null
  const code = ['gantt', '    dateFormat YYYY-MM-DD', '    axisFormat %m/%d',
                '    todayMarker stroke:#059669,stroke-width:2px', ...rows].join('\n')
  return <div className="plan-gantt"><Mermaid code={code} /></div>
}

function PlanDays({ items }: { items: Plan['items'] }) {
  const today = new Date().toLocaleDateString('sv')   // sv locale 就是 YYYY-MM-DD
  const days = new Map<string, Plan['items']>()
  for (const item of [...items].sort((a, b) => a.due_date.localeCompare(b.due_date))) {
    days.set(item.due_date, [...(days.get(item.due_date) ?? []), item])
  }
  return <div className="plan-days">
    {[...days].map(([date, dayItems]) => {
      const when = date === today ? '今天' : date < today ? '已过' : ''
      return <section className={`plan-day ${date === today ? 'is-today' : ''} ${date < today ? 'is-past' : ''}`} key={date}>
        <h3>{date.slice(5).replace('-', ' / ')}{when && <em>{when}</em>}<span>{dayItems.length} 项</span></h3>
        {dayItems.map(item => <div className="material-row" key={item.id}>
          <div className="material-copy"><b>{item.title}</b><small>{item.status}{item.concept_name ? ` · ${item.concept_name}` : ''}</small></div>
        </div>)}
      </section>
    })}
  </div>
}

// 只让 http(s) 变成可点链接。后端已经挡了伪协议，前端再挡一次——
// 这个值最终进 href，两边都不该信对方。
function safeHref(url?: string): string | null {
  if (!url) return null
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null
  } catch { return null }
}

function CitationChip({ item, fallbackNumber, onOpen }: { item: Citation; fallbackNumber: number; onOpen: (citation: Citation) => void }) {
  const label = <><i>[{item.number ?? fallbackNumber}]</i>{item.kind === 'web'
    ? (item.title || item.url || '网页')
    : `${item.material_name ?? '资料'}${item.page ? `:${item.page}` : ''}`}</>
  const href = item.kind === 'web' ? safeHref(item.url) : null
  if (href) return <a className="citation-web" href={href} target="_blank" rel="noopener noreferrer nofollow" title={item.url}>{label}</a>
  return <button onClick={() => onOpen(item)}>{label}</button>
}

function MessageCard({ message, onCitation, showResolution, onRetry }: { message: Message; onCitation: (citation: Citation) => void; showResolution: boolean; onRetry?: () => void }) {
  if (message.role === 'user') return <article className="message user-message"><div>{message.content}</div></article>
  const isInterrupted = message.artifact?.kind === 'interrupted' || message.status === 'interrupted'
  // 课程会话的课程是固定的，逐条标注解析结果只会制造噪音；仅通用会话展示。
  const resolution = !showResolution ? null : message.resolution_status === 'resolved' ? `本轮解析：${message.resolved_course_name ?? message.resolved_course_id ?? '课程'}` : message.resolution_status ? '本轮未解析课程' : null
  return <article className="message assistant-message"><div className="agent-label"><span aria-hidden>❯</span><b>CoursePilot</b></div>{message.activity && message.activity.length > 0 && <div className="tool-activity">{message.activity.map(entry => <ToolChip key={entry.call_id} entry={entry} />)}</div>}
    {message.status === 'stopped' && <div className="degraded-notice"><span>已停止。上面的内容没有存进会话记录。</span>{onRetry && <button type="button" className="ghost-button" onClick={onRetry}>重发这个问题</button>}</div>}
    {message.degraded && <div className="degraded-notice">{message.degraded}。这次回答没有用教材检索与工具，仅供参考。</div>}<div className={message.status === 'streaming' ? 'message-content streaming' : 'message-content'}>{message.content ? <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={markdownComponents}>{message.content}</ReactMarkdown> : <ThinkingHint activity={message.activity} />}</div>{resolution && <span className={`message-resolution ${message.resolution_status === 'resolved' ? 'resolved' : ''}`}>{resolution}</span>}{isInterrupted && <div className="interrupted"><span>回答中断了，已生成的部分保留在上面。</span>{onRetry && <button type="button" className="ghost-button" onClick={onRetry}>重发这个问题</button>}</div>}{message.citations && message.citations.length > 0 && <div className="citations"><span className="refs-label">SOURCES · {message.citations.length}</span>{message.citations.map((item, index) => <CitationChip key={`${item.id ?? item.chunk_id ?? item.url ?? index}`} item={item} fallbackNumber={index + 1} onOpen={onCitation} />)}</div>}{message.artifact && message.artifact.visibility !== 'model_private' && message.artifact.kind !== 'interrupted' && <div className="artifact-card"><b>公开学习内容</b><span>{message.artifact.kind}</span></div>}</article>
}

function LibraryView({ course, onCourseChange, onError }: { course: Course; onCourseChange: (course: Course) => void; onError: (message: string) => void }) {
  const [tab, setTab] = useState<'rag' | 'wiki' | 'notes'>('rag'); const [materials, setMaterials] = useState<Material[]>([]); const [jobs, setJobs] = useState<Record<string, Job>>({}); const [searchQuery, setSearchQuery] = useState(''); const [results, setResults] = useState<SearchResult[]>([]); const [loading, setLoading] = useState(false); const fileInput = useRef<HTMLInputElement>(null)
  const [ragBackend, setRagBackend] = useState<string>('')
  const reload = async () => { try { setMaterials(await api.materials(course.id)) } catch (error) { onError(errorText(error)) } }
  const removeMaterial = async (materialId: string) => {
    try { await api.deleteMaterial(materialId); await reload() }
    catch (error) { onError(errorText(error)) }
  }
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
  return <section className="page"><div className="page-inner"><div className="hero"><div><p className="eyebrow">知识仓库</p><h1 className="course-heading"><i style={{ backgroundColor: course.color }} />{course.name}</h1><p>这门课的教材、索引与检索都在这里。换课程用左栏。{backendLabel && <span className="backend-badge">{backendLabel}</span>}</p></div><div className="hero-actions"><button className="ghost-button" onClick={() => void reload()}>刷新状态</button></div></div><div className="tabs"><button className={tab === 'rag' ? 'active' : ''} onClick={() => setTab('rag')}>RAG 资料库</button><button className={tab === 'wiki' ? 'active' : ''} onClick={() => setTab('wiki')}>Wiki 知识页 {course.wiki_enabled ? '' : '（已关闭）'}</button><button className={tab === 'notes' ? 'active' : ''} onClick={() => setTab('notes')}>课程笔记</button></div>
    {tab === 'notes' && <NotesPanel course={course} onError={onError} />}
    {tab === 'rag' ? <><div className="library-grid"><article className="card upload-card"><h2>上传教材</h2><p>支持 PDF、TXT、MD。上传后自动执行：解析文本 → 切块 → 生成语义向量 → 建立索引。</p><input ref={fileInput} type="file" accept=".pdf,.txt,.md,text/plain,application/pdf,text/markdown" onChange={upload} hidden /><button className="primary-button" onClick={() => fileInput.current?.click()} disabled={loading}>上传到「{course.name}」</button><small>单个教材 ≤ 100 MiB，对话图片 ≤ 10 MiB。</small></article><article className="card search-card"><h2>检索验证</h2><p>在「{course.name}」范围内试查，看看索引质量与能引用的片段。</p><form onSubmit={search}><input value={searchQuery} onChange={event => setSearchQuery(event.target.value)} placeholder="试试概念名或一个真实问题" /><button className="primary-button" disabled={loading}>检索</button></form></article></div><article className="card material-card"><div className="card-heading"><div><h2>资料与索引</h2><p>进度来自后端任务。</p></div><button className="text-button" onClick={() => void reload()}>刷新</button></div>{materials.length ? materials.map(material => <MaterialRow material={material} jobs={jobs} key={material.id} onReindex={reindex} onDelete={removeMaterial} />) : <div className="empty-inline">还没有资料。上传并索引完成后可以在这里试查。</div>}</article>{results.length > 0 && <article className="card results-card"><h2>检索结果</h2>{results.map((result, index) => <div className="result" key={result.id ?? result.chunk_id ?? index}><b>{result.material_name ?? '资料片段'} {result.page ? `· p.${result.page}` : ''}</b><p>{result.text ?? '服务端未返回可展示的文本片段。'}</p><small>{result.score !== undefined ? `检索排序分 ${result.score.toFixed(4)}` : '已返回引用'}</small></div>)}</article>}</> : <article className="card wiki-card"><div className="switch-row"><div><h2>启用 Course Wiki <span>实验功能</span></h2><p>关闭后不再生成新页面，已有的页面不会删除，提问与检索不受影响。</p></div><button className={`switch ${course.wiki_enabled ? 'on' : ''}`} aria-label="切换 Course Wiki" onClick={toggleWiki}><i /></button></div>{course.wiki_enabled ? <><p className="wiki-note">选一份已索引的资料，开始「提取目录 → 概念候选 → 页面草稿 → 待确认」。</p>{indexedMaterials.length ? indexedMaterials.map(material => {
      const wikiJob = Object.values(jobs).find(item => item.material_id === material.id && item.type === 'wiki')
      const running = wikiJob ? !['completed', 'failed'].includes(wikiJob.status) : false
      return <div className="material-row" key={material.id}><div className="file-mark">{fileKind(material)}</div><div className="material-copy"><b>{material.filename ?? material.name ?? '未命名资料'}</b><small>{wikiJob ? (STAGE_LABELS[String(wikiJob.stage ?? wikiJob.status)] ?? String(wikiJob.status)) : '已索引，可独立解析到 Wiki'}</small>{wikiJob && <div className="job-progress"><i style={{ width: `${wikiJob.progress ?? 15}%` }} /></div>}{wikiJob?.error && <small className="danger-text">{wikiJob.error}</small>}</div><button className="ghost-button" onClick={() => void buildWiki(material.id)} disabled={running}>{wikiJob && !running ? '重新解析到 Wiki' : '解析到 Wiki'}</button></div>
    }) : <div className="empty-inline">请先上传并完成至少一份资料的索引。</div>}</> : <div className="empty-inline"><b>Wiki 尚未启用</b><p>Wiki 用来浏览教材生成的知识页。不开也不影响提问与检索。</p></div>}</article>}</div></section>
}

const STAGE_LABELS: Record<string, string> = { uploaded: '待索引', queued: '排队中', starting: '准备中', extracting: '解析文本', chunking: '切块', embedding: '生成语义向量', indexing: '建立索引', completed: '已索引', indexed: '已索引', indexing_failed: '失败', failed: '失败', reading_index: '读取索引', wiki_completed: 'Wiki 已生成' }
const INDEX_PIPELINE: [string, string][] = [['extracting', '解析'], ['chunking', '切块'], ['embedding', '向量'], ['indexing', '索引']]

function MaterialRow({ material, jobs, onReindex, onDelete }: { material: Material; jobs: Record<string, Job>; onReindex: (materialId: string) => void; onDelete?: (materialId: string) => Promise<void> }) {
  const [confirming, setConfirming] = useState(false)
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
    {!jobActive && onDelete && <button className="text-button danger-text" onClick={() => setConfirming(true)}>删除</button>}
    <span className={`status-tag ${failed ? 'failed' : ''}`}>{statusLabel}</span>
    {confirming && onDelete && <DangerConfirm
      what={`教材「${material.filename ?? material.name ?? '未命名资料'}」`}
      consequences={[
        '这份教材的原文件、切块与索引',
        '由它提取的概念，以及基于这些概念的掌握度',
        '答题记录本身会保留，掌握度日后可以从记录重算',
      ]}
      onConfirm={() => { setConfirming(false); void onDelete(material.id) }}
      onCancel={() => setConfirming(false)} />}
  </div>
}
function fileKind(material: Material) { const name = material.filename ?? material.name ?? ''; return name.split('.').pop()?.toUpperCase().slice(0, 4) || 'FILE' }

function PlanView({ course, onError }: { course: Course; onError: (message: string) => void }) {
  const [plan, setPlan] = useState<Plan | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    setPlan(null); setLoaded(false); setError('')
    api.plan(course.id).then(payload => setPlan(payload.plan)).catch(error => { setError(errorText(error)); onError(errorText(error)) }).finally(() => setLoaded(true))
  }, [course.id])
  return <section className="page"><div className="page-inner">
    <div className="hero"><div><p className="eyebrow">学习计划</p><h1 className="course-heading"><i style={{ backgroundColor: course.color }} />{course.name}</h1><p>在对话里说要排计划或改计划，助手就会写到这里。每次改动升一个版本，过去的条目不动。</p></div></div>
    {!loaded ? <p className="mini-empty">正在读取计划…</p> : error ? <RetryCard title="计划读取失败" message={error} onRetry={() => { setLoaded(false); setError('') }} /> : plan ? <article className="card"><div className="card-heading"><div><h2>当前计划</h2><p>版本 v{plan.version} · {plan.items.length} 个条目 · 更新于 {plan.updated_at.slice(0, 16).replace('T', ' ')}</p></div></div><PlanGantt items={plan.items} /><PlanDays items={plan.items} /></article> : <article className="card"><h2>还没有学习计划</h2><p>告诉助手考试日期和复习范围，让它排一份计划，这里就会显示。</p></article>}
  </div></section>
}
function ArchiveView({ course, onError }: { course: Course; onError: (message: string) => void }) {
  const [archive, setArchive] = useState<ArchiveSummary | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    setArchive(null); setLoaded(false); setError('')
    api.archive(course.id).then(setArchive).catch(error => { setError(errorText(error)); onError(errorText(error)) }).finally(() => setLoaded(true))
  }, [course.id])
  return <section className="page"><div className="page-inner">
    <div className="hero"><div><p className="eyebrow">学习档案</p><h1 className="course-heading"><i style={{ backgroundColor: course.color }} />{course.name}</h1><p>答题记录只增不改，掌握度由这些记录算出。</p></div></div>
    {!loaded ? <p className="mini-empty">正在读取档案…</p> : error ? <RetryCard title="档案读取失败" message={error} onRetry={() => { setLoaded(false); setError('') }} /> : !archive ? <p className="mini-empty">暂无档案数据。</p> : <>
      <article className="card"><div className="card-heading"><div><h2>概念掌握度</h2><p>BKT 后验 × 遗忘曲线；证据不足的概念不给判断</p></div></div>
        {archive.mastery.length ? archive.mastery.map(item => <div className="material-row" key={item.concept_id}>
          <div className="file-mark">{item.insufficient_evidence ? '—' : `${Math.round((item.score ?? 0) * 100)}`}</div>
          <div className="material-copy"><b>{item.name}</b>
            <small>{item.insufficient_evidence ? `数据不足（${item.objective_events} 条客观证据）` : `${item.objective_events} 条客观证据`}{item.due_at ? ` · 复习到期 ${item.due_at.slice(0, 10)}` : ''}</small>
            {!item.insufficient_evidence && <div className="job-progress"><i style={{ width: `${Math.round((item.score ?? 0) * 100)}%` }} /></div>}
          </div>
        </div>) : <div className="empty-inline">还没有掌握度数据。做几道练习并提交作答，这里就会按概念显示。</div>}
      </article>
      <article className="card"><div className="card-heading"><div><h2>证据事件</h2><p>共 {archive.evidence_count} 条</p></div></div>{archive.events.length ? archive.events.map(event => <div className="material-row" key={event.id}><div className="file-mark">{event.kind.toUpperCase().slice(0, 4)}</div><div className="material-copy"><b>{event.concept_name ?? event.topic_hint ?? (event.concept_id ? "已归因概念" : "未归因")}</b><small>{event.attribution_status} · {timeLabel(event.created_at)}</small></div></div>) : <div className="empty-inline">还没有证据事件。答题、小测与纠错之后，这里会出现可追溯的记录。</div>}</article>
      {archive.unattributed.length > 0 && <article className="card"><div className="card-heading"><div><h2>未归因主题</h2><p>模型认不出概念时留下的线索，可以手动补进概念目录</p></div></div>
        {archive.unattributed.map(item => <div className="material-row" key={item.topic_hint}><div className="file-mark">{item.hits}</div><div className="material-copy"><b>{item.topic_hint}</b><small>最近 {timeLabel(item.last_seen)}</small></div></div>)}
      </article>}
    </>}
  </div></section>
}

/** 长期记忆：user.md 与课程 memory.md 此前只有文件、没有入口，而文档宣称"可读可编辑"。 */
function MemoryCard({ courses, onError }: { courses: Course[]; onError: (message: string) => void }) {
  const [scope, setScope] = useState<string>('user')
  const [content, setContent] = useState('')
  const [draft, setDraft] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [saving, setSaving] = useState(false)
  const courseId = scope === 'user' ? undefined : scope
  useEffect(() => {
    setLoaded(false)
    api.memory(courseId)
      .then(payload => { setContent(payload.content); setDraft(payload.content) })
      .catch(error => onError(errorText(error)))
      .finally(() => setLoaded(true))
  }, [scope])
  async function save() {
    setSaving(true)
    try {
      const payload = await api.saveMemory(draft, courseId)
      setContent(payload.content); setDraft(payload.content)
    } catch (error) { onError(errorText(error)) } finally { setSaving(false) }
  }
  const dirty = draft !== content
  return <article className="card"><h2>长期记忆</h2>
    <p>助手跨课程记住的偏好与目标写在 <code>user.md</code>，每门课的学习进展写在各自的
      <code>memory.md</code>。掌握度、错题与复习排期不在这里，那些由证据事件维护。</p>
    <div className="memory-head">
      <select value={scope} onChange={event => setScope(event.target.value)} aria-label="选择记忆范围">
        <option value="user">跨课程画像（user.md）</option>
        {courses.map(course => <option value={course.id} key={course.id}>{course.name} · 课程记忆</option>)}
      </select>
      <span>{dirty ? '有未保存的修改' : loaded ? '已是最新' : '读取中…'}</span>
      <button className="ghost-button" disabled={!dirty || saving} onClick={() => void save()}>{saving ? '保存中…' : '保存'}</button>
      <button className="ghost-button" disabled={!dirty} onClick={() => setDraft(content)}>放弃修改</button>
    </div>
    <textarea className="memory-editor" value={draft} onChange={event => setDraft(event.target.value)}
      placeholder={loaded ? '还没有内容。助手会在对话里自动补写，你也可以直接在这里写。' : '读取中…'}
      spellCheck={false} aria-label="记忆内容" />
    <p className="help-note">带 <code>agent:managed</code> 标记的区块由助手维护，删掉标记它会重新追加一份；
      标记之外的段落助手不会覆盖。</p>
  </article>
}

const SKILL_STATUS: Record<string, string> = { enabled: '已启用', draft: '未启用', permission_denied: '权限不足' }

function SkillsCard({ onError }: { onError: (message: string) => void }) {
  const [skills, setSkills] = useState<SkillInfo[] | null>(null)
  const [importable, setImportable] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const folderInput = useRef<HTMLInputElement>(null)
  const [skipped, setSkipped] = useState<string[]>([])
  async function reload() {
    try { const payload = await api.skills(); setSkills(payload.skills); setImportable(payload.importable_tools) }
    catch (error) { setSkills([]); onError(errorText(error)) }
  }
  useEffect(() => { void reload() }, [])
  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    try { await action(); await reload() } catch (error) { onError(errorText(error)) } finally { setBusy(false) }
  }
  async function pick(event: ChangeEvent<HTMLInputElement>) {
    const files = [...event.target.files ?? []]; event.target.value = ''
    if (!files.length) return
    setSkipped([])
    await run(async () => { setSkipped((await api.importSkill(files)).skipped_files ?? []) })
  }
  return <article className="card"><h2>能力（Skill）</h2>
    <p>导入的 skill 默认关着，预览之后再打开。能授予的工具只有：{importable.join('、') || '—'}。</p>
    {skills === null ? <p className="empty-inline">正在读取…</p> : skills.map(skill => <div className="skill-row" key={skill.name}>
      <div className="skill-copy">
        <b>{skill.name}<em>{skill.origin === 'builtin' ? '内建' : '导入'}</em></b>
        <small>{skill.when_to_use}</small>
        <small className="skill-tools">工具：{skill.allowed_tools.join('、') || '—'}</small>
        {skill.denied_tools.length > 0 && <small className="skill-denied">被拒：{skill.denied_tools.join('、')} —— 改好 allowed_tools 再导入一次才能启用</small>}
      </div>
      <div className="skill-actions">
        <span className={`skill-status ${skill.status}`}>{SKILL_STATUS[skill.status] ?? skill.status}</span>
        {skill.origin === 'user' && <>
          <button className="ghost-button" disabled={busy || skill.status === 'permission_denied'} onClick={() => void run(() => api.setSkillEnabled(skill.name, skill.status !== 'enabled'))}>{skill.status === 'enabled' ? '停用' : '启用'}</button>
          <button className="ghost-button danger" disabled={busy} onClick={() => void run(() => api.deleteSkill(skill.name))}>删除</button>
        </>}
      </div>
    </div>)}
    <div className="skill-import">
      <button className="ghost-button" disabled={busy} onClick={() => fileInput.current?.click()}>导入文件（.md / .zip）</button>
      <button className="ghost-button" disabled={busy} onClick={() => folderInput.current?.click()}>导入文件夹</button>
    </div>
    <small className="help-note">规程带的参考文件（.md / .txt / .json / .yaml / .csv）会一起并进规程；脚本与二进制文件跳过——这里不执行命令。</small>
    {skipped.length > 0 && <small className="skill-denied">已跳过：{skipped.join('、')}</small>}
    <input ref={fileInput} type="file" accept=".md,.zip,text/markdown,application/zip" hidden onChange={pick} />
    <input ref={folderInput} type="file" hidden multiple onChange={pick} {...{ webkitdirectory: '' }} />
  </article>
}

function SettingsView({ courses, onError, onCourseDeleted }: { courses: Course[]; onError: (message: string) => void; onCourseDeleted: (courseId: string) => void }) {
  const [health, setHealth] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(false)
  async function check() { setLoading(true); try { setHealth(await api.health()) } catch (error) { onError(errorText(error)) } finally { setLoading(false) } }
  const llm = (health?.llm ?? null) as Record<string, unknown> | null
  const rag = (health?.rag ?? null) as Record<string, unknown> | null
  const embedding = (rag?.embedding ?? null) as Record<string, unknown> | null
  return <section className="page"><div className="page-inner"><div className="hero"><div><h1>管理与设置</h1><p>课程、能力（Skill）与服务状态分开管理。</p></div><button className="ghost-button" onClick={check} disabled={loading}>检查服务</button></div><div className="settings-grid"><article className="card"><h2>课程与教材</h2><p>共 {courses.length} 门课程。课程颜色由服务端稳定返回。</p>{courses.length ? courses.map(course => <CourseSettingRow key={course.id} course={course} onDelete={onCourseDeleted} onError={onError} />) : <p className="empty-inline">暂无课程，请从左栏创建。</p>}</article><MemoryCard courses={courses} onError={onError} /><SkillsCard onError={onError} /><article className="card health-card"><h2>运行状态</h2>{health ? <><dl>
    <div><dt>回答模型</dt><dd>{llm ? `${String(llm.provider)} / ${String(llm.model)} · ${llm.enabled ? '远端已启用' : '本地 Demo responder'}` : '未知'}</dd></div>
    <div><dt>检索方式</dt><dd>{rag?.backend === 'hybrid_bge' ? '语义 + 词面混合' : '仅词面'}</dd></div>
    {embedding && <div><dt>向量模型</dt><dd>{String(embedding.model)} · {embedding.error ? `加载失败：${String(embedding.error)}` : embedding.loaded ? '已加载' : '待首次使用时加载'}</dd></div>}
    <div><dt>数据库</dt><dd>{(health.database as Record<string, unknown>)?.ok ? `正常 · migration v${String((health.database as Record<string, unknown>)?.migration_version)}` : '异常'}</dd></div>
  </dl><details><summary>原始 JSON</summary><pre>{JSON.stringify(health, null, 2)}</pre></details></> : <p>点「检查服务」看模型与检索的当前状态。</p>}</article></div></div></section>
}
function CoursePickerState({ view, courses, onPick, onCreate }: { view: View; courses: Course[]; onPick: (courseId: string) => void; onCreate: () => void }) {
  return <section className="page"><div className="page-inner empty-course"><span aria-hidden>❯</span><h1>先选择一个课程</h1><p>{viewNames[view]}以课程为边界。选择后左栏会跟着切过去。</p>
    <div className="picker-grid">{courses.map(item => <button className="picker-card" key={item.id} onClick={() => onPick(item.id)}><i style={{ backgroundColor: item.color }} /><b>{item.name}</b>{item.wiki_enabled && <em>Wiki</em>}</button>)}<button className="picker-card picker-create" onClick={onCreate}>＋ 新建课程</button></div>
  </div></section>
}
function CitationDrawer({ citation, onClose }: { citation: Citation; onClose: () => void }) { return <aside className="citation-drawer" role="dialog" aria-label="教材引用详情"><header><div><p>教材引用</p><h2>{citation.material_name ?? '资料片段'}</h2></div><button aria-label="关闭引用详情" onClick={onClose}>×</button></header><p className="citation-location">{citation.page ? `第 ${citation.page} 页` : citation.chunk_id ? `片段 ${citation.chunk_id}` : '服务端返回的资料定位'}</p><blockquote>{citation.text ?? '该引用未提供可展示的原文片段。'}</blockquote>{citation.score !== undefined && <p>检索排序分：{citation.score.toFixed(4)}</p>}</aside> }
