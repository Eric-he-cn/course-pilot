import { ChangeEvent, FormEvent, ReactElement, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
import { api, clearCurrentUser, currentModel, currentThinking, currentUser, onConnectionLost, setCurrentModel, setCurrentThinking, setCurrentUser } from './api'
import { getLang, LangContext, LANGS, locale, nameParts, setLang, t, tOr, useI18n, type Lang } from './i18n'
import type { ArchiveSummary, Attachment, Citation, CitationSource, ConceptNode, ContextUsage, Course, Job, Material, MaterialStructure, Message, MistakeRecord, Plan, ScopeMode, SearchResult, NoteSummary, OcrEstimate, SessionSummary, SkillInfo, StructurePreview, ToolActivity, WikiEstimate, WikiPageSummary } from './types'

type View = 'chat' | 'library' | 'plan' | 'archive' | 'settings' | 'help'
type Workspace = { scope: ScopeMode; courseId?: string }
type TurnResolution = { sessionId: string; status: string; courseId: string | null; courseName: string | null }

function viewName(view: View) { return t(`nav.${view}`) }
const nav: { id: View; num: string }[] = [
  { id: 'chat', num: '01' }, { id: 'library', num: '02' }, { id: 'plan', num: '03' }, { id: 'archive', num: '04' },
]
const MAX_MATERIAL_BYTES = 100 * 1024 * 1024
// 会用到本地嵌入/重排模型的工具：只有这几个会因为模型加载而变慢。
const RETRIEVAL_TOOLS = ['search_materials', 'concept_search']
// 停止时给未收尾的工具补的占位。带上 key 而不只是当时那句中文，展示层才判得出它不是真结果。
const TOOL_STOPPED_KEY = 'tool.stopped'
// 工具名在字典里（`tool.<name>`）。这张表是全部工具，使用说明按它的顺序列举。
const TOOL_CAPABILITY_HINT: Record<string, string> = {
  search_materials: 'read_course', list_materials: 'read_course', get_plan: 'read_course',
  get_archive: 'read_course', concept_search: 'read_course', note_read: 'read_course',
  history_read: 'read_course', wiki_index: 'read_course', wiki_read: 'read_course',
  plan_update: 'write_state', emit_evidence: 'write_state', memory_patch: 'write_state',
  artifact_append: 'write_state', note_write: 'write_note',
  web_search: 'network', web_fetch: 'network',
  use_skill: 'free', artifact_read: 'free', calculator: 'free', ask_user: 'free',
  delegate: 'delegate',
}

function errorText(error: unknown) { return error instanceof Error ? error.message : t('error.unknown') }
function timeLabel(value?: string) { return value ? new Intl.DateTimeFormat(locale(), { hour: '2-digit', minute: '2-digit', month: 'numeric', day: 'numeric' }).format(new Date(value)) : t('time.just_now') }

function randomNames(count = 5): string[] {
  const { adjectives, creatures } = nameParts()
  const picked = new Set<string>()
  while (picked.size < count) {
    picked.add(adjectives[Math.floor(Math.random() * adjectives.length)] + creatures[Math.floor(Math.random() * creatures.length)])
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
    if (!value) { setError(t('login.error.empty')); return }
    if (value.length > 32) { setError(t('login.error.too_long')); return }
    if (!/^[\p{L}\p{N} _-]+$/u.test(value)) { setError(t('login.error.charset')); return }
    onLogin(value)
  }
  return <div className="login-screen">
    <form className="login-card" onSubmit={submit}>
      <div className="brand"><div className="brandmark">{'>_'}</div><div className="brand-copy"><strong>CoursePilot</strong><span className="ver">v2.0</span></div></div>
      <h1>{t('login.title')}</h1>
      <p>{t('login.intro.before')}<strong>{t('login.intro.strong')}</strong>{t('login.intro.after')}</p>
      <input value={name} autoFocus aria-label={t('a11y.username')} placeholder={t('login.placeholder')}
        onChange={event => { setName(event.target.value); setError('') }} />
      {error && <span className="login-error">{error}</span>}
      <div className="login-suggestions">
        <span>{t('login.suggest')}</span>
        {suggestions.map(item => <button type="button" key={item} onClick={() => { setName(item); setError('') }}>{item}</button>)}
      </div>
      <button className="login-submit" type="submit">{remembered && remembered === name.trim() ? t('login.continue_as', { name: remembered }) : t('login.enter')}</button>
      {remembered && <small>{t('login.last_used', { name: remembered })}</small>}
    </form>
  </div>
}

export default function App() {
  const [username, setUsername] = useState(() => currentUser())
  // 语言存在 i18n 的模块变量里（api.ts 也要读），这份 state 只负责换语言后重渲染整棵树。
  const [lang, setLangState] = useState<Lang>(() => getLang())
  const [courses, setCourses] = useState<Course[]>([])
  const [workspace, setWorkspace] = useState<Workspace>({ scope: 'general' })
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [activeSession, setActiveSession] = useState<SessionSummary | null>(null)
  // 流是异步的，用户随时可能切走。回调里要拿「此刻」在看哪个会话，闭包捕获的值不行。
  const activeSessionRef = useRef<SessionSummary | null>(null)
  activeSessionRef.current = activeSession
  const [messages, setMessages] = useState<Message[]>([])
  const [view, setView] = useState<View>('chat')
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => localStorage.getItem('cp-sidebar-collapsed') === 'true')
  // 生成回答与新建课程/会话分开：一次回答要跑一分钟，这一分钟里不该连侧栏都点不动。
  const [streaming, setStreaming] = useState(false)
  const [creating, setCreating] = useState(false)
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

  const i18n = useMemo(() => ({ lang, setLang: (next: Lang) => { setLang(next); setLangState(next) } }), [lang])
  const course = useMemo(() => courses.find(item => item.id === workspace.courseId) ?? null, [courses, workspace.courseId])
  const heading = activeSession?.title && view === 'chat' ? activeSession.title : viewName(view)

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
    // 请求回来时用户可能已经切走：先发的请求后到，会把别的会话的消息盖上去。
    try { const loaded = await api.messages(id); if (activeSessionRef.current?.id === id) setMessages(loaded) }
    catch (error) { if (activeSessionRef.current?.id === id) setMessages([]); setNotice(errorText(error)) }
  }
  // keepView：从某个页面内选课程时留在当前页，不要弹回对话。
  function switchWorkspace(next: Workspace, options: { keepView?: boolean } = {}) {
    setWorkspace(next)
    if (!options.keepView) setView('chat')
    setSidebarOpen(false); setCitation(null); setTurnResolution(null); setContextUsage(null)
  }
  async function newSession() {
    setCreating(true)
    try {
      const session = await api.createSession(workspace.scope, workspace.courseId)
      setSessions(current => [session, ...current]); setActiveSession(session); setView('chat'); setMessages([])
    } catch (error) { setNotice(errorText(error)) } finally { setCreating(false) }
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
    const name = window.prompt(t('course.prompt_name'))?.trim(); if (!name) return
    setCreating(true)
    try { const created = await api.createCourse(name); setCourses(current => [...current, created]); switchWorkspace({ scope: 'course', courseId: created.id }) }
    catch (error) { setNotice(errorText(error)) } finally { setCreating(false) }
  }

  if (!username) return <LoginView onLogin={name => { setCurrentUser(name); setUsername(name); window.location.reload() }} />

  const workspaceName = workspace.scope === 'general' ? t('workspace.general') : course?.name ?? t('workspace.course_fallback')
  const healthLlm = (health?.llm ?? null) as Record<string, unknown> | null
  const healthRag = (health?.rag ?? null) as Record<string, unknown> | null
  // 冷启动时首次检索要等模型加载（实测嵌入 36s、重排 60s）。不说清楚，用户只看到
  // 「正在检索教材」干等一分钟，会当成检索本身慢。配了但还没加载好的才算。
  const modelNote = (['embedding', 'reranker'] as const)
    .filter(key => {
      const slot = healthRag?.[key] as { model?: string; loaded?: boolean; error?: string | null } | undefined
      return !!slot?.model && slot.loaded === false && !slot.error
    })
    .map(key => (key === 'embedding' ? t('model.embedding') : t('model.reranker')))
    .join(t('common.list_sep'))
  return <LangContext.Provider value={i18n}><div className={`app ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
    {sidebarOpen && <button className="sidebar-backdrop" aria-label={t('a11y.close_nav')} onClick={() => setSidebarOpen(false)} />}
    <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`} aria-label={t('a11y.sidebar')}>
      <div className="brand"><div className="brandmark">{'>_'}</div><div className="brand-copy"><strong>CoursePilot</strong><span className="ver">v2.0</span></div></div>
      <div className="side-label">WORKSPACE</div>
      <button className={`workspace-card ${workspace.scope === 'general' ? 'selected' : ''}`} onClick={() => switchWorkspace({ scope: 'general' })}>
        <span className="general-icon" aria-hidden><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><circle cx="12" cy="12" r="9" /><path d="M12 3v18M3 12h18" opacity=".35" /></svg></span>
        <span className="workspace-copy"><b>{t('workspace.general')}</b><small>{t('workspace.general_hint')}</small></span>
      </button>
      <div className="course-switcher">
        {courses.map(item => <button className={`course-choice ${item.id === workspace.courseId ? 'selected' : ''}`} key={item.id} onClick={() => switchWorkspace({ scope: 'course', courseId: item.id })}>
          <i style={{ backgroundColor: item.color }} /><span>{item.name}</span>{item.wiki_enabled && <em>Wiki</em>}
        </button>)}
        <button className="text-button add-course" onClick={createCourse} disabled={creating}>{t('course.new')}</button>
      </div>
      <div className="side-label">NAV</div>
      <nav className="main-nav" aria-label={t('a11y.main_nav')}>
        {nav.map(item => <button className={view === item.id ? 'active' : ''} key={item.id} onClick={() => { setView(item.id); setSidebarOpen(false) }}><span aria-hidden>{item.num}</span><b>{viewName(item.id)}</b></button>)}
      </nav>
      <div className="sessions-head"><span>SESSIONS</span></div>
      <div className="session-list">
        {sessions.length ? sessions.map(session => <SessionRow key={session.id} session={session}
          active={session.id === activeSession?.id}
          onOpen={() => { setActiveSession(session); setView('chat'); setSidebarOpen(false) }}
          onRename={async title => { await renameSession(title, session.id) }}
          onDelete={async () => { await deleteSession(session) }} />) : <p className="mini-empty">{t('session.empty')}</p>}
      </div>
      <button className="new-session" onClick={newSession} disabled={creating}>{t('session.new', { scope: workspace.scope === 'general' ? t('session.scope_general') : t('session.scope_course') })}</button>
      <div className="sidebar-foot">
        <button onClick={() => { setView('help'); setSidebarOpen(false) }}>? <span>{t('nav.help')}</span></button>
        <button onClick={() => { clearCurrentUser(); window.location.reload() }} title={t('a11y.current_user', { name: username })}>⇄ <span>{t('user.switch', { name: username })}</span></button>
        <button onClick={() => { setView('settings'); setSidebarOpen(false) }}>⚙ <span>{t('nav.settings')}</span></button>
      </div>
    </aside>
    <main className="main">
      <header className="topbar">
        <button className="icon-button mobile-only" aria-label={t('a11y.open_nav')} onClick={() => setSidebarOpen(true)}>☰</button>
        <button className="icon-button collapse-only" aria-label={t('a11y.toggle_sidebar')} onClick={() => setSidebarCollapsed(value => !value)}>☷</button>
        <div className="title-area">
          {view === 'chat' && activeSession
            ? <SessionTitle session={activeSession} onRename={renameSession} />
            : <b>{heading}</b>}
          <span className="crumb"><i style={{ backgroundColor: course?.color ?? '#D4D4D8' }} /> {workspaceName}</span>
        </div>
      </header>
      {notice && <div className="notice" role="alert"><span>{notice}</span><button aria-label={t('a11y.dismiss_notice')} onClick={() => setNotice('')}>×</button></div>}
      {view === 'chat' && <ChatView session={activeSession} messages={messages} workspaceName={workspaceName} scope={workspace.scope} modelNote={modelNote} turnResolution={turnResolution} contextUsage={contextUsage} draftSeed={draftSeed} onSeedUsed={() => setDraftSeed('')} onCitation={setCitation} onUpload={async file => {
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
          setStreaming(true)
          try {
            targetSession = await api.createSession(workspace.scope, workspace.courseId)
            // ref 同步跟上：setActiveSession 要等下一次渲染，中间到达的流式片段会被守卫误丢。
            activeSessionRef.current = targetSession
            setSessions(current => [targetSession!, ...current]); setActiveSession(targetSession); setView('chat'); setMessages([])
          } catch (error) { setNotice(errorText(error)); setStreaming(false); return }
        }
        const optimistic: Message = { id: `pending-user-${Date.now()}`, role: 'user', content }
        const pendingId = `pending-assistant-${Date.now()}`
        const activity: ToolActivity[] = []
        // 这一轮的产出只属于 targetSession。用户中途切走后，界面上是别的会话，
        // 再往 messages 里写就成了「B 的标题配 A 的对话」。
        const onThisSession = () => activeSessionRef.current?.id === targetSession.id
        const patchMessages: typeof setMessages = updater => { if (onThisSession()) setMessages(updater) }
        // 停止/失败时没有 tool_result 收尾，chip 会一直停在 pending 上自己数秒。
        // 换成新对象而不是原地改：chip 的重渲染就不必依赖「没人给它加 memo」。
        const sealActivity = () => {
          activity.forEach((item, index) => {
            if (item.summary) return
            activity[index] = { ...item, summary: t(TOOL_STOPPED_KEY), summary_key: TOOL_STOPPED_KEY, elapsed_ms: item.started_at ? Date.now() - item.started_at : undefined }
          })
          return [...activity]
        }
        setMessages(current => [...current, optimistic, { id: pendingId, role: 'assistant', content: '', status: 'streaming' }]); setStreaming(true)
        const controller = new AbortController()
        abortRef.current = controller
        stoppedRef.current = false
        try { await api.turn(targetSession.id, content, payload => {
          const resolved = payload.type === 'course_resolution' || payload.event === 'course_resolution'
          if (resolved) {
            const isResolved = payload.status === 'resolved'
            const resolvedId = isResolved ? payload.resolved_course_id ?? payload.course_id ?? null : null
            setTurnResolution({ sessionId: targetSession.id, status: payload.status ?? 'unresolved', courseId: resolvedId, courseName: isResolved ? payload.course_name ?? null : null })
            if (onThisSession()) setActiveSession(current => current ? { ...current, resolved_course_id: resolvedId, course_name: isResolved ? payload.course_name ?? current.course_name : null, course_color: isResolved ? payload.course_color ?? current.course_color : null } : current)
            setSessions(current => current.map(item => item.id === targetSession.id ? { ...item, resolved_course_id: resolvedId, course_name: isResolved ? payload.course_name ?? item.course_name : null, course_color: isResolved ? payload.course_color ?? item.course_color : null } : item))
          }
          if (payload.type === 'context_usage' && payload.segments) {
            if (onThisSession()) setContextUsage({ segments: payload.segments, total_tokens: payload.total_tokens ?? 0, limit_tokens: payload.limit_tokens ?? 1, history_budget_tokens: payload.history_budget_tokens ?? 0, dropped_history: payload.dropped_history ?? 0, clipped_history: payload.clipped_history ?? 0, compacted_messages: payload.compacted_messages ?? 0, clipped_segments: payload.clipped_segments ?? [], gate_tools_cleared: payload.gate_tools_cleared ?? 0, gate_history_dropped: payload.gate_history_dropped ?? 0, gate_evidence_clipped: payload.gate_evidence_clipped ?? false })
          }
          if (payload.type === 'tool_call' && payload.call_id) {
            activity.push({ call_id: payload.call_id, name: payload.name ?? t('tool.fallback_name'), origin: payload.origin, started_at: Date.now() })
            patchMessages(current => current.map(item => item.id === pendingId ? { ...item, activity: [...activity] } : item))
          }
          if (payload.type === 'tool_result' && payload.call_id) {
            const entry = activity.find(item => item.call_id === payload.call_id)
            if (entry) { entry.summary = payload.summary; entry.summary_key = payload.summary_key; entry.summary_args = payload.summary_args; entry.ok = payload.ok; entry.elapsed_ms = entry.started_at ? Date.now() - entry.started_at : undefined }
            patchMessages(current => current.map(item => item.id === pendingId ? { ...item, activity: [...activity] } : item))
          }
          if (payload.type === 'text_delta' && payload.text) {
            const delta = payload.text
            patchMessages(current => current.map(item => item.id === pendingId ? { ...item, content: item.content + delta } : item))
          }
          if (payload.type === 'provider_fallback') {
            // 远端模型不可用时会静默切到本地兜底（无工具、无检索）。不上屏的话，
            // 用户会把质量完全不同的回答当成正常回答。
            patchMessages(current => current.map(item => item.id === pendingId ? { ...item, degraded: t('chat.degraded_switch', { provider: payload.provider ?? '' }) } : item))
          }
          if (payload.type === 'choices' && payload.options?.length) {
            const options = payload.options
            patchMessages(current => current.map(item => item.id === pendingId ? { ...item, choices: options } : item))
          }
          if (payload.type === 'turn_completed' && payload.finish_reason === 'length') setNotice(t('chat.length_limit'))
        }, attachmentIds, controller.signal)
          if (stoppedRef.current) {
            // 客户端断连时服务端生成器可能挂在 yield 上不进 finally，部分回答不一定落盘，
            // 所以保留本地已渲染的内容并标明它没有保存。
            patchMessages(current => current.map(item => item.id === pendingId
              ? { ...item, status: 'stopped', content: item.content || t('chat.stopped_empty'), activity: sealActivity() }
              : item))
            await loadSessions()
          } else {
            await loadMessages(targetSession.id); await loadSessions()
          }
        }
        catch (error) {
          if (stoppedRef.current) {
            patchMessages(current => current.map(item => item.id === pendingId
              ? { ...item, status: 'stopped', content: item.content || t('chat.stopped_empty'), activity: sealActivity() }
              : item))
            void loadSessions()
            return
          }
          setNotice(errorText(error))
          // 优先回读服务端真值（部分回答已带 interrupted 状态持久化）；服务不可达时保留本地标记。
          try { await loadMessages(targetSession.id); await loadSessions() }
          catch { patchMessages(current => current.map(item => item.id === pendingId ? { ...item, content: item.content || t('chat.interrupted_retry'), artifact: { kind: 'interrupted' }, activity: sealActivity() } : item)) }
        }
        finally { setStreaming(false); abortRef.current = null }
      }} busy={streaming} onStop={() => { stoppedRef.current = true; abortRef.current?.abort() }} />}
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
        <LangPicker />
        {apiOnline !== false && healthRag && <span className="statusbar-detail">{retrievalLabel(healthRag.backend as string | undefined, true)}</span>}
        {apiOnline !== false && view === 'chat' && <span className="statusbar-detail">{t('status.retrieval_note')}</span>}
        <span className="right">CoursePilot v2.0</span>
      </footer>
    </main>
    {citation && <CitationDrawer citation={citation} onClose={() => setCitation(null)} onOpen={setCitation} />}
  </div></LangContext.Provider>
}

function ChatView({ session, messages, workspaceName, scope, modelNote, turnResolution, contextUsage, draftSeed, onSeedUsed, onCitation, onUpload, onSend, onStop, busy }: { session: SessionSummary | null; messages: Message[]; workspaceName: string; scope: ScopeMode; modelNote?: string; turnResolution: TurnResolution | null; contextUsage: ContextUsage | null; draftSeed: string; onSeedUsed: () => void; onStop: () => void; onCitation: (citation: Citation) => void; onUpload: (file: File) => Promise<Attachment>; onSend: (content: string, attachmentIds: string[]) => Promise<void>; busy: boolean }) {
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
    following.current = true  // 自己刚发的消息一定要看得见，哪怕之前翻上去过
    setDraft(''); setAttachments([]); await onSend(text, ids)
  }
  const scroller = useRef<HTMLDivElement>(null)
  // 用户是否还贴着底部。要在滚动时记，不能等到 effect 里量：那时新消息已经把
  // 容器撑高了，一条长回答会被误判成「用户翻上去了」。
  const following = useRef(true)
  const lastContent = messages.length ? messages[messages.length - 1].content.length : 0
  // 换会话就贴到最新一条：不控制的话滚动位置由渲染时序决定，同一个会话两次进去可能停在不同地方。
  useEffect(() => {
    const box = scroller.current
    if (box) box.scrollTop = box.scrollHeight
    following.current = true
  }, [session?.id])
  // 新消息与流式追加都跟着走，但用户手动往上翻了就别把他拽回来。
  useEffect(() => {
    const box = scroller.current
    if (box && following.current) box.scrollTop = box.scrollHeight
  }, [messages.length, lastContent])

  const contextNote = !session ? t('chat.no_session')
    : session.scope_mode !== 'general' ? ''
    : turnResolution?.sessionId === session.id
      ? (turnResolution.status === 'resolved'
          ? t('chat.resolved_this_turn', { course: turnResolution.courseName ?? turnResolution.courseId ?? '' })
          : t('chat.unresolved_hint'))
      : session.resolved_course_id ? t('chat.resolved_recent', { course: session.course_name ?? session.resolved_course_id }) : ''
  return <section className="chat-view">
    {/* 课程会话的会话名与课程顶栏已经显示了，这里只留通用会话才有的逐轮解析结果。 */}
    {contextNote && <div className="session-context">{contextNote}</div>}
    <div className="messages" aria-live="polite" ref={scroller}
      onScroll={event => { const box = event.currentTarget; following.current = box.scrollHeight - box.scrollTop - box.clientHeight < 120 }}>
      {!messages.length && <div className="welcome"><span aria-hidden>❯</span><h1>{t('chat.welcome_title')}</h1><p>{isCourseScope ? t('chat.welcome_course', { name: workspaceName }) : t('chat.welcome_general')}</p><div className="suggestion-row">{(isCourseScope ? [t('chat.suggest.concepts'), t('chat.suggest.practice'), t('chat.suggest.plan')] : [t('chat.suggest.concept_general'), t('chat.suggest.practice'), t('chat.suggest.plan')]).map(text => <button key={text} className="suggestion-chip" onClick={() => { setDraft(text); composer.current?.focus() }}>{text}</button>)}</div></div>}
      {messages.filter(item => item.role !== 'system').map((message, index, list) => <MessageCard message={message} key={message.id} onCitation={onCitation} modelNote={modelNote} onChoose={busy ? undefined : text => void onSend(text, [])} showResolution={!isCourseScope}
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
          <span className="attach-preview">{item.needs_confirmation ? t('attach.no_text') : item.transcription}</span>
          <button type="button" aria-label={t('a11y.remove_image', { name: item.filename })} onClick={() => setAttachments(current => current.filter(other => other.id !== item.id))}>×</button>
        </div>)}
        {uploading && <div className="attach-chip pending"><span className="attach-name">IMG</span><span className="attach-preview">{t('attach.transcribing')}</span></div>}
      </div>}
      <div className="composer"><span className="prompt" aria-hidden>❯</span><textarea ref={composer} value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit() } }} placeholder={session ? t('chat.composer_placeholder') : t('chat.composer_placeholder_empty')} aria-label={t('a11y.composer')} rows={2} /><div className="composer-row"><button type="button" className="attach-button" onClick={() => fileInput.current?.click()} disabled={busy || uploading} aria-label={t('a11y.upload_image')}><svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden><rect x="1.5" y="2.5" width="13" height="11" rx="1.5" /><circle cx="5.5" cy="6.5" r="1.2" /><path d="M2.5 12.5 6.5 9l3 2.5 2-1.5 2 2" /></svg>{t('chat.image_button')}</button><span>{t('chat.enter_hint')}</span>{contextUsage && <ContextMeter usage={contextUsage} />}{busy
      ? <button className="send-button stop" type="button" onClick={onStop} aria-label={t('a11y.stop')} title={t('a11y.stop')}>■</button>
      : <button className="send-button" type="submit" disabled={!draft.trim() || uploading} aria-label={t('a11y.send')}>↑</button>}</div></div>
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
    anchor.download = t('diagram.filename', { date: new Date().toISOString().slice(0, 10) })
    anchor.click()
    URL.revokeObjectURL(url)
  }

  if (!svg) return <pre className={failed ? 'mermaid-source failed' : 'mermaid-source'}>
    <span className="mermaid-hint">{failed ? t('diagram.bad_syntax') : t('diagram.rendering')}</span>
    <code>{code}</code>
  </pre>
  return <figure className="mermaid-figure">
    <div role="img" aria-label={t('a11y.diagram')} dangerouslySetInnerHTML={{ __html: svg }} />
    <figcaption>
      <button type="button" onClick={download}>{t('diagram.download')}</button>
      <span>{t('diagram.open_hint')}</span>
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


// 分组名与说明在字典里（`capability.<key>` 与 `.hint`）：模块顶层调 t() 会锁住加载时的语言。
const CAPABILITY_GROUPS = ['read_course', 'write_state', 'write_note', 'network', 'delegate', 'free'] as const

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
    { done: courses.length > 0, title: t('help.step1'), hint: t('help.step1_hint') },
    { done: indexedCourses.length > 0, title: t('help.step2'), hint: t('help.step2_hint') },
    { done: hasSession, title: t('help.step3'), hint: t('help.step3_hint') },
  ]
  const grouped = CAPABILITY_GROUPS.map(key => ({
    key,
    label: t(`capability.${key}`),
    hint: t(`capability.${key}.hint`),
    tools: Object.keys(TOOL_CAPABILITY_HINT).filter(name => TOOL_CAPABILITY_HINT[name] === key).map(name => tOr(`tool.${name}`, name)),
  })).filter(group => group.tools.length > 0)

  return <section className="page"><div className="page-inner">
    <div className="hero"><div><p className="eyebrow">{t('nav.help')}</p><h1>{t('help.title')}</h1>
      <p>{t('help.subtitle')}</p></div></div>

    <article className="card"><h2>{t('help.steps_title')}</h2>
      <p>{t('help.steps_hint')}</p>
      {steps.map((step, index) => <div className={`help-step ${step.done ? 'done' : ''}`} key={step.title}>
        <i aria-hidden>{step.done ? '✓' : index + 1}</i>
        <div><b>{step.title}</b><small>{step.hint}</small></div>
      </div>)}
    </article>

    <article className="card"><h2>{t('help.modes_title')}</h2>
      <div className="help-columns">
        <div><b>{t('help.mode_general')}</b><p>{t('help.mode_general.before')}<strong>{t('help.mode_general.strong')}</strong>{t('help.mode_general.after')}</p></div>
        <div><b>{t('help.mode_course')}</b><p>{t('help.mode_course_body')}</p></div>
      </div>
    </article>

    <article className="card"><h2>{skills ? t('help.skills_title_n', { n: skills.length }) : t('help.skills_title')}</h2>
      <p>{t('help.skills_hint')}</p>
      {skills === null ? <p className="mini-empty">{t('common.loading')}</p> : skills.filter(item => item.status === 'enabled').map(skill => <div className="help-skill" key={skill.name}>
        <div className="help-skill-head"><b>{skill.name}</b><span>{skill.origin === 'builtin' ? t('skill.origin_builtin') : t('skill.origin_user')}</span></div>
        <p>{skill.description}</p>
        <small>{t('help.when_to_use', { text: skill.when_to_use })}</small>
        {skill.examples && skill.examples.length > 0 && <div className="help-examples">
          {skill.examples.map(example => <button type="button" key={example} onClick={() => onTry(example)}>{example}</button>)}
        </div>}
      </div>)}
    </article>

    <article className="card"><h2>{t('help.instance_title')}</h2>
      <dl className="help-facts">
        <div><dt>{t('help.fact_model')}</dt><dd>{llm ? `${String(llm.provider)} / ${String(llm.model)}${llm.enabled ? '' : t('help.llm_local_note')}` : t('common.unknown')}</dd></div>
        <div><dt>{t('help.fact_retrieval')}</dt><dd>{retrievalLabel(rag?.backend as string | undefined)}</dd></div>
        <div><dt>{t('help.fact_web')}</dt><dd>{web && (web as Record<string, unknown>).enabled ? t('help.web_on') : t('help.web_off')}</dd></div>
        <div><dt>{t('help.fact_limits')}</dt><dd>{t('help.limits_body')}</dd></div>
      </dl>
    </article>

    <article className="card"><h2>{t('help.reach_title')}</h2>
      <p>{t('help.reach_hint')}</p>
      {grouped.map(group => <div className="help-group" key={group.key}>
        <div><b>{group.label}</b><small>{group.hint}</small></div>
        <span>{group.tools.join(t('common.list_sep'))}</span>
      </div>)}
      <p className="help-note">{t('help.reach_note')}</p>
    </article>

    <article className="card"><h2>{t('help.not_title')}</h2>
      <p>{t('help.not_body')}</p>
    </article>
  </div></section>
}

function CourseSettingRow({ course, onDelete, onError }: {
  course: Course; onDelete: (courseId: string) => void; onError: (message: string) => void
}) {
  const [confirming, setConfirming] = useState(false)
  return <div className="settings-course">
    <i style={{ backgroundColor: course.color }} /><b>{course.name}</b>
    <span>{course.wiki_enabled ? t('settings.wiki_on') : t('settings.wiki_off')}</span>
    <button className="text-button danger-text" onClick={() => setConfirming(true)}>{t('common.delete')}</button>
    {confirming && <DangerConfirm
      what={t('settings.delete_course_what', { name: course.name })}
      consequences={[
        t('settings.delete_course.c1'),
        t('settings.delete_course.c2'),
        t('settings.delete_course.c3'),
        t('settings.delete_course.c4'),
        t('settings.delete_course.c5'),
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
    <b>{t('danger.title', { what })}</b>
    <ul>{consequences.map(line => <li key={line}>{line}</li>)}</ul>
    <div className="danger-actions">
      <button className="danger" onClick={onConfirm}>{t('danger.confirm')}</button>
      <button onClick={onCancel}>{t('common.cancel')}</button>
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
    <input className="session-rename" value={draft} autoFocus aria-label={t('a11y.session_title')}
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
    <span>{t('session.delete_confirm')}</span>
    <button className="danger" onClick={() => { setMode('idle'); void onDelete() }}>{t('common.delete')}</button>
    <button onClick={() => setMode('idle')}>{t('common.cancel')}</button>
  </div>

  return <div className={`session-row ${active ? 'active' : ''}`}>
    <button className="session" onClick={onOpen}>
      <i title={session.scope_mode === 'general' ? t('session.general') : t('session.course')} style={{ backgroundColor: session.course_color ?? '#D4D4D8' }} /><span className="session-text"><b>{session.title || t('session.untitled')}</b><small>{timeLabel(session.updated_at)}</small></span>
    </button>
    <span className="session-actions">
      <button aria-label={t('a11y.rename_session')} title={t('common.rename')} onClick={() => setMode('rename')}>✎</button>
      <button aria-label={t('a11y.delete_session')} title={t('common.delete')} onClick={() => setMode('confirm')}>×</button>
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
      <span className="sr-only">{t('picker.model')}</span>
      <select value={active.key} onChange={event => { setCurrentModel(event.target.value); setModel(event.target.value) }}>
        {options.map(item => <option key={item.key} value={item.key}>{item.label} · {item.model}</option>)}
      </select>
    </label>
    <label className="statusbar-picker">
      <span className="sr-only">{t('picker.thinking')}</span>
      <select value={mode} onChange={event => apply(event.target.value === 'on' ? effort : event.target.value)}>
        <option value="off">{t('picker.thinking_off')}</option>
        <option value="adaptive">{t('picker.thinking_auto')}</option>
        <option value="on">{t('picker.thinking_on')}</option>
      </select>
    </label>
    <label className="statusbar-picker">
      <span className="sr-only">{t('picker.effort')}</span>
      <select value={effort} disabled={mode !== 'on'} onChange={event => apply(event.target.value)}>
        <option value="high">{t('picker.effort_high')}</option>
        <option value="max">{t('picker.effort_max')}</option>
      </select>
    </label>
  </>
}

/** 界面语言。只换外壳文案，模型回答仍按提问语言。 */
function LangPicker() {
  const { lang, setLang: apply } = useI18n()
  return <label className="statusbar-picker">
    <span className="sr-only">{t('picker.language')}</span>
    <select value={lang} onChange={event => apply(event.target.value as Lang)}>
      {LANGS.map(item => <option key={item} value={item}>{t(`lang.${item}`)}</option>)}
    </select>
  </label>
}

function ThinkingHint({ activity, modelNote }: { activity?: ToolActivity[]; modelNote?: string }) {
  // 工具跑完到第一个字之间有一段空档，这里不说话用户就以为卡在上一个工具上。
  const running = activity?.find(entry => !entry.summary)
  const label = running ? t('thinking.running', { tool: tOr(`tool.${running.name}`, running.name) }) : activity?.length ? t('thinking.thinking') : t('thinking.preparing')
  // 检索类工具在等模型加载时把原因说出来，别让用户以为是检索慢。
  const reason = modelNote && running && RETRIEVAL_TOOLS.includes(running.name) ? t('thinking.model_loading', { models: modelNote }) : ''
  return <span className="typing">{label}{reason}<i aria-hidden /><i aria-hidden /><i aria-hidden /></span>
}

/** 种子检索是服务端每轮自动做的，只有命中才值得占一行——它解释了引用从哪来。
 *  进行中由「正在检索教材」那句兜着，未命中和停止占位都不上屏。
 *  模型自己发起的检索照常显示，失败的那一步也是它的决策过程。 */
function seedChipHidden(entry: ToolActivity): boolean {
  if (entry.origin !== 'seed') return false
  return !entry.summary || entry.summary_key === 'summary.search_miss' || entry.summary_key === TOOL_STOPPED_KEY
}

function ToolActivityRow({ activity }: { activity: ToolActivity[] }) {
  const visible = activity.filter(entry => !seedChipHidden(entry))
  if (visible.length === 0) return null
  return <div className="tool-activity">{visible.map(entry => <ToolChip key={entry.call_id} entry={entry} />)}</div>
}

function ToolChip({ entry }: { entry: ToolActivity }) {
  const pending = !entry.summary
  // 后端给了 key 就用本地译文；历史 activity 只有中文 summary，退回它。
  const translated = entry.summary_key ? tOr(entry.summary_key, entry.summary ?? '', entry.summary_args) : entry.summary
  const summary = translated && entry.reused ? `${translated}${t('chip.reused_suffix')}` : translated
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
    <span className="sr-only">{pending ? t('chip.pending') : entry.ok === false ? t('chip.failed') : t('chip.done')}</span>
    {tOr(`tool.${entry.name}`, entry.name)}{summary ? ` · ${summary}` : ''}{timing}
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
    <div className="card-heading"><div><h2>{t('library.notes_title')}</h2>
      <p>{t('library.notes_hint')}</p></div></div>
    {notes === null ? <p className="mini-empty">{t('common.loading')}</p> : notes.length === 0
      ? <div className="empty-inline"><b>{t('library.notes_empty_title')}</b><p>{t('library.notes_empty_body')}</p></div>
      : notes.map(note => <div className="material-row" key={note.title}>
          <div className="file-mark">MD</div>
          <div className="material-copy"><b>{note.title}</b><small>{t('library.note_meta', { chars: note.chars, time: note.updated_at.slice(0, 16).replace('T', ' ') })}</small></div>
          <button className="ghost-button" onClick={() => void read(note.title)}>{t('common.view')}</button>
        </div>)}
    {open && <div className="note-viewer">
      <div className="note-viewer-head"><b>{open.title}</b><button onClick={() => setOpen(null)} aria-label={t('a11y.close_note')}>×</button></div>
      <div className="message-content"><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={markdownComponents}>{open.content}</ReactMarkdown></div>
    </div>}
  </article>
}

/** 可折叠树的一个节点。概念目录与知识页共用，各自把自己的行映射成这个形状。 */
interface TreeItem { id: string; parentId: string; label: string; meta: string; onOpen?: () => void }

/** 按 parent_id 分组。父节点不在列表里的当作根节点，不硬造一层假的根。
 *  知识页的 frontmatter 用户可以手改，改成父子互指时环里的节点从根走不到，
 *  整棵树会少画几行还不出声。这里把够不到的节点提回根，一行都不少。 */
function groupByParent(items: TreeItem[]): Map<string, TreeItem[]> {
  const grouped = new Map<string, TreeItem[]>()
  const known = new Set(items.map(item => item.id))
  const push = (key: string, item: TreeItem) => grouped.set(key, [...(grouped.get(key) ?? []), item])
  for (const item of items) push(item.parentId && known.has(item.parentId) ? item.parentId : '', item)
  const rooted = new Set<string>()
  const absorb = (roots: TreeItem[]) => {
    const queue = [...roots]
    while (queue.length) {
      const item = queue.shift()!
      if (rooted.has(item.id)) continue
      rooted.add(item.id)
      queue.push(...(grouped.get(item.id) ?? []))
    }
  }
  absorb(grouped.get('') ?? [])
  for (const item of items) {
    if (rooted.has(item.id)) continue
    // 断掉这一条回边，环就成了以它为根的一棵树，它的子孙也跟着走得到了。
    grouped.set(item.parentId, (grouped.get(item.parentId) ?? []).filter(kid => kid.id !== item.id))
    push('', item)
    absorb([item])
  }
  return grouped
}

/** 共用的行渲染：缩进按深度，有子项的才画折叠箭头。兄弟顺序沿用传入顺序。 */
function treeRows(children: Map<string, TreeItem[]>, collapsed: Set<string>, toggle: (id: string) => void,
                  parentId = '', depth = 0): ReactElement[] {
  return (children.get(parentId) ?? []).flatMap(item => {
    const kids = children.get(item.id) ?? []
    const shut = collapsed.has(item.id)
    return [
      <div className="concept-row" key={item.id} style={{ paddingLeft: `${depth * 18}px` }}>
        {kids.length > 0
          ? <button type="button" className={shut ? 'concept-toggle' : 'concept-toggle open'} aria-expanded={!shut} aria-label={item.label} onClick={() => toggle(item.id)}>›</button>
          : <span className="concept-bullet" aria-hidden />}
        {item.onOpen ? <button type="button" className="tree-open" onClick={item.onOpen}>{item.label}</button> : <b>{item.label}</b>}
        <small>{item.meta}{kids.length > 0 && ` · ${t('library.concepts_children', { n: kids.length })}`}</small>
      </div>,
      ...(shut ? [] : treeRows(children, collapsed, toggle, item.id, depth + 1)),
    ]
  })
}

/** 折叠状态与「全部展开/折叠」按钮。两个树面板的行为要一致，写一处。 */
function useCollapse(resetKey: unknown) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  useEffect(() => { setCollapsed(new Set()) }, [resetKey])
  const toggle = (id: string) =>
    setCollapsed(current => { const next = new Set(current); next.has(id) ? next.delete(id) : next.add(id); return next })
  const toggleAll = (branches: TreeItem[]) =>
    setCollapsed(collapsed.size ? new Set() : new Set(branches.map(item => item.id)))
  return { collapsed, toggle, toggleAll }
}

/** 概念目录：层级来自教材自带的目录书签，没有书签就平铺。 */
function ConceptTreePanel({ course, refreshKey, onError }: { course: Course; refreshKey: number; onError: (message: string) => void }) {
  const [nodes, setNodes] = useState<ConceptNode[] | null>(null)
  const { collapsed, toggle, toggleAll } = useCollapse(`${course.id}:${refreshKey}`)
  const { lang } = useI18n()
  useEffect(() => {
    setNodes(null)
    api.concepts(course.id).then(payload => setNodes(payload.concepts)).catch(error => { setNodes([]); onError(errorText(error)) })
  }, [course.id, refreshKey])
  // 后端按目录顺序返回，这里只按 parent_id 分组，兄弟节点的先后原样保留。
  // lang 要进依赖：memo 里调了 t()，换语言时数据没变，缓存的旧译文会和现算的部分混排。
  const items = useMemo(() => (nodes ?? []).map(node => ({
    id: node.id, parentId: node.parent_id ?? '', label: node.name,
    meta: node.page ? t('library.concepts_page', { page: node.page }) : t('library.concepts_no_page'),
  })), [nodes, lang])
  const children = useMemo(() => groupByParent(items), [items])
  const branches = useMemo(() => items.filter(item => (children.get(item.id) ?? []).length > 0), [items, children])
  return <article className="card">
    <div className="card-heading">
      <div><h2>{nodes ? t('library.concepts_title_n', { n: nodes.length }) : t('library.concepts_title')}</h2>
        <p>{t('library.concepts_hint')}</p></div>
      {branches.length > 0 && <button className="text-button" onClick={() => toggleAll(branches)}>
        {collapsed.size ? t('library.concepts_expand_all') : t('library.concepts_collapse_all')}</button>}
    </div>
    {nodes === null ? <p className="mini-empty">{t('common.loading')}</p>
      : nodes.length === 0 ? <div className="empty-inline">{t('library.concepts_empty')}</div>
        : <div className="concept-tree">
            {branches.length === 0 && <p className="wiki-note">{t('library.concepts_flat_note')}</p>}
            {treeRows(children, collapsed, toggle)}
          </div>}
  </article>
}

/** 目录结构：概念与层级单独一条流水线，重算不重新提取也不重新向量化。
 *  没有层级的教材在这里说清楚，并给出重新解析的入口。 */
function StructurePanel({ course, refreshKey, onError, onParsed }: {
  course: Course; refreshKey: number; onError: (message: string) => void; onParsed: () => void
}) {
  const [rows, setRows] = useState<MaterialStructure[] | null>(null)
  const [target, setTarget] = useState<MaterialStructure | null>(null)
  const [preview, setPreview] = useState<StructurePreview | null>(null)
  const [running, setRunning] = useState(false)
  const [done, setDone] = useState<{ added: number; removed: number } | null>(null)
  useEffect(() => {
    setRows(null); setTarget(null)
    api.structure(course.id).then(payload => setRows(payload.materials)).catch(error => { setRows([]); onError(errorText(error)) })
  }, [course.id, refreshKey])
  // 重算的结果只属于这门课。不能跟着 refreshKey 一起清——重算完 refreshKey 就变，那句话会来不及看见。
  useEffect(() => { setDone(null) }, [course.id])
  function ask(row: MaterialStructure) {
    // 预告要现算，所以点开就发请求，拿到再显示数字
    setTarget(row); setPreview(null); setDone(null)
    api.previewStructure(row.material_id).then(setPreview).catch(error => { onError(errorText(error)); setTarget(null) })
  }
  async function confirm() {
    if (!target) return
    setRunning(true)
    try {
      const result = await api.parseStructure(target.material_id)
      setDone({ added: result.added, removed: result.removed }); setTarget(null); onParsed()
    } catch (error) { onError(errorText(error)) } finally { setRunning(false) }
  }
  function status(row: MaterialStructure): string {
    if (!row.has_structure) return row.index_status === 'indexed' ? t('library.structure_none') : t('library.structure_not_indexed')
    return t(row.has_levels ? 'library.structure_leveled' : 'library.structure_flat', { n: row.concepts })
  }
  return <article className="card">
    <div className="card-heading"><div><h2>{t('library.structure_title')}</h2>
      <p>{t('library.structure_hint')}</p></div></div>
    {done && <p className="help-note">{done.added || done.removed
      ? t('library.structure_done', { added: done.added, removed: done.removed })
      : t('library.structure_done_none')}</p>}
    {rows === null ? <p className="mini-empty">{t('common.loading')}</p>
      : rows.length === 0 ? <div className="empty-inline">{t('library.materials_empty')}</div>
        : rows.map(row => <div className="material-row" key={row.material_id}>
            <div className="file-mark">TOC</div>
            <div className="material-copy"><b>{row.filename}</b><small>{status(row)}</small>
              {row.has_structure && !row.has_levels && <small className="wiki-note">{t('library.structure_flat_note')}</small>}</div>
            <button className="ghost-button" disabled={row.index_status !== 'indexed' || running} onClick={() => ask(row)}>
              {row.has_structure ? t('library.structure_reparse') : t('library.structure_parse')}</button>
          </div>)}
    {target && <StructurePreviewPanel filename={target.filename} preview={preview} running={running}
      onConfirm={() => void confirm()} onCancel={() => setTarget(null)} />}
  </article>
}

/** 重建目录结构的影响预告。删概念会连带删掉掌握度与错题，先说清楚再让用户点。 */
function StructurePreviewPanel({ filename, preview, running, onConfirm, onCancel }: {
  filename: string; preview: StructurePreview | null; running: boolean
  onConfirm: () => void; onCancel: () => void
}) {
  const shown = (preview?.removed_names ?? []).slice(0, 8)
  const rest = (preview?.removed_names.length ?? 0) - shown.length
  const names = [...shown, ...(rest > 0 ? [t('structure.and_more', { n: rest })] : [])].join(t('common.list_sep'))
  return <article className="card ocr-card">
    <h2>{t('structure.title', { name: filename })}</h2>
    <p>{t('structure.body')}</p>
    {preview === null ? <p className="mini-empty">{t('structure.loading')}</p>
      : preview.empty ? <p className="danger-text">{t('structure.empty')}</p>
        : <>
            <p>{t('structure.result', { added: preview.added, kept: preview.kept, removed: preview.removed })}</p>
            {preview.owned_elsewhere > 0 && <p className="help-note">{t('structure.owned_elsewhere', { n: preview.owned_elsewhere })}</p>}
            {preview.removed > 0 && <p className="help-note">{t('structure.removed_list', { names })}</p>}
            <p className={preview.at_risk > 0 ? 'danger-text' : 'help-note'}>
              {preview.at_risk > 0 ? t('structure.at_risk', { n: preview.at_risk }) : t('structure.safe')}</p>
            <small className="help-note">{preview.has_levels ? t('structure.levels_yes') : t('structure.levels_no')}</small>
          </>}
    <div className="danger-actions">
      <button className="primary-button" disabled={preview === null || running} onClick={onConfirm}>
        {running ? t('structure.running') : t('structure.confirm')}</button>
      <button className="ghost-button" onClick={onCancel}>{t('common.cancel')}</button>
    </div>
  </article>
}

/** frontmatter 是给追溯用的元数据，不该当正文渲染给用户看。 */
function stripFrontmatter(raw: string): string {
  const match = /^---\n[\s\S]*?\n---\n/.exec(raw)
  return match ? raw.slice(match[0].length).trimStart() : raw
}

/** 已生成的 Wiki 页，按教材目录嵌成一棵可折叠的树。没有层级的教材照旧平铺。
 *  正文里的 [p.N] 与 frontmatter 的 source_refs 对得上。 */
function WikiPagesPanel({ course, refreshKey, onError }: { course: Course; refreshKey: number; onError: (message: string) => void }) {
  const [pages, setPages] = useState<WikiPageSummary[] | null>(null)
  const [open, setOpen] = useState<{ title: string; content: string } | null>(null)
  const { collapsed, toggle, toggleAll } = useCollapse(`${course.id}:${refreshKey}`)
  const { lang } = useI18n()
  useEffect(() => {
    setPages(null); setOpen(null)
    api.wikiPages(course.id).then(payload => setPages(payload.pages)).catch(error => { setPages([]); onError(errorText(error)) })
  }, [course.id, refreshKey])
  async function read(page: WikiPageSummary) {
    try {
      const raw = (await api.wikiPage(course.id, page.concept_id)).content
      setOpen({ title: page.concept_name, content: stripFrontmatter(raw) })
    } catch (error) { onError(errorText(error)) }
  }
  // lang 要进依赖：memo 里调了 t()，换语言时数据没变，缓存的旧译文会和现算的部分混排。
  const items = useMemo(() => (pages ?? []).map(page => ({
    id: page.concept_id, parentId: page.parent_id ?? '', label: page.concept_name,
    meta: t('library.updated_at', { time: page.updated_at.slice(0, 16).replace('T', ' ') }),
    onOpen: () => void read(page),
  })), [pages, lang])
  const children = useMemo(() => groupByParent(items), [items])
  const branches = useMemo(() => items.filter(item => (children.get(item.id) ?? []).length > 0), [items, children])
  if (pages !== null && pages.length === 0) return null
  return <article className="card">
    <div className="card-heading">
      <div><h2>{pages ? t('library.wiki_pages_title_n', { n: pages.length }) : t('library.wiki_pages_title')}</h2>
        <p>{t('library.wiki_pages_hint')}</p></div>
      {branches.length > 0 && <button className="text-button" onClick={() => toggleAll(branches)}>
        {collapsed.size ? t('library.concepts_expand_all') : t('library.concepts_collapse_all')}</button>}
    </div>
    {pages === null ? <p className="mini-empty">{t('common.loading')}</p>
      : <div className="concept-tree">
          {branches.length === 0 && <p className="wiki-note">{t('library.wiki_pages_flat_note')}</p>}
          {treeRows(children, collapsed, toggle)}
        </div>}
    {open && <div className="note-viewer">
      <div className="note-viewer-head"><b>{open.title}</b><button onClick={() => setOpen(null)} aria-label={t('a11y.close_wiki')}>×</button></div>
      <div className="message-content"><ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={markdownComponents}>{open.content}</ReactMarkdown></div>
    </div>}
  </article>
}

/** 检索方式的显示名。三档判定只在这里做，short 供状态栏这类窄位置用。
 *  后端没上报 backend 时返回空串，由调用方决定不显示。 */
function retrievalLabel(backend: string | undefined, short = false): string {
  if (!backend) return ''
  if (backend === 'hybrid_bge_rerank') return short ? t('retrieval.hybrid_rerank_short') : t('retrieval.hybrid_rerank')
  if (backend === 'hybrid_bge') return short ? t('retrieval.hybrid_short') : t('retrieval.hybrid')
  return short ? t('retrieval.lexical_short') : t('retrieval.lexical')
}

function RetryCard({ title, message, onRetry }: { title: string; message: string; onRetry: () => void }) {
  return <article className="card"><h2>{title}</h2><p>{message}</p>
    <button className="ghost-button" onClick={onRetry}>{t('common.reload')}</button>
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
    className="title-input" value={draft} autoFocus aria-label={t('a11y.session_title')}
    onChange={event => setDraft(event.target.value)}
    onBlur={() => void commit()}
    onKeyDown={event => {
      if (event.key === 'Enter') { event.preventDefault(); void commit() }
      if (event.key === 'Escape') { setDraft(session.title); setEditing(false) }
    }} />
  return <button type="button" className="title-edit" onClick={() => setEditing(true)} title={t('a11y.click_rename')}>
    <b>{session.title || t('session.untitled')}</b>
    <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden><path d="M11.5 2.5l2 2-7.5 7.5-2.5.5.5-2.5z" /></svg>
  </button>
}

function ContextMeter({ usage }: { usage: ContextUsage }) {
  const [open, setOpen] = useState(false)
  const k = (tokens: number) => tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}K` : String(tokens)
  const percent = Math.min(100, Math.round((usage.total_tokens / usage.limit_tokens) * 100))
  const filled = Math.max(1, Math.round(percent / 12.5))
  const gated = usage.gate_tools_cleared > 0 || usage.gate_history_dropped > 0 || usage.gate_evidence_clipped
  const notice = usage.dropped_history > 0 || usage.clipped_history > 0 || usage.clipped_segments.length > 0 || gated
  return <div className="context-chip">
    <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} aria-label={t('a11y.context')} className={notice ? 'warn' : undefined}>
      <span aria-hidden>{'▓'.repeat(filled)}{'░'.repeat(8 - filled)}</span>
      <b>{percent}%</b>
    </button>
    {open && <div className="context-popover">
      <div className="popover-head"><b>{t('context.title')}</b><span>{k(usage.total_tokens)} / {k(usage.limit_tokens)}</span></div>
      {usage.segments.map(segment => <div className="popover-row" key={segment.label}><span>{segment.label_key ? tOr(segment.label_key, segment.label) : segment.label}</span><b>{k(segment.tokens)}</b></div>)}
      <p>{t('context.note')}</p>
      {usage.compacted_messages > 0 && <p className="popover-note">{t('context.compacted', { n: usage.compacted_messages })}</p>}
      {usage.dropped_history > 0 && <p className="popover-warn">{t('context.dropped', { n: usage.dropped_history })}</p>}
      {usage.clipped_history > 0 && <p className="popover-warn">{t('context.clipped', { n: usage.clipped_history })}</p>}
      {usage.clipped_segments.map(item => <p className="popover-warn" key={item.label_key ?? item.label}>
        {t('context.over_quota', { name: item.label_key ? tOr(item.label_key, item.label) : item.label, before: k(item.before), after: k(item.after) })}
      </p>)}
      {usage.gate_tools_cleared > 0 && <p className="popover-warn">{t('context.gate_tools', { n: usage.gate_tools_cleared })}</p>}
      {usage.gate_history_dropped > 0 && <p className="popover-warn">{t('context.gate_history', { n: usage.gate_history_dropped })}</p>}
      {usage.gate_evidence_clipped && <p className="popover-warn">{t('context.gate_evidence')}</p>}
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
    if (index !== week) { week = index; rows.push(`    section ${t('plan.gantt_section', { n: index + 1 })}`) }
    // mermaid 用冒号和逗号分隔字段，而计划标题里这两样都很常见——不换掉整张图都画不出来。
    const label = item.title.replace(/[:：,，#;]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 16) || t('plan.untitled_item')
    const state = item.status === 'done' ? 'done'
      : item.due_date === today ? 'active'
      : item.due_date < today ? 'crit' : ''
    rows.push(`    ${label} :${state}${state ? ', ' : ''}${item.due_date}, 1d`)
  }
  if (!rows.length) return null
  // useWidth 让 mermaid 按这个宽度自己排版。不设的话它按内容算出一个很窄的尺寸，
  // 任务名会挤成一团；而用 CSS 去拉宽会连文字一起放大。
  const code = ['%%{init: {"gantt": {"useWidth": 900, "leftPadding": 96, "barHeight": 18, "fontSize": 12}}}%%',
                'gantt', '    dateFormat YYYY-MM-DD', '    axisFormat %m/%d',
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
      const when = date === today ? t('plan.today') : date < today ? t('plan.past') : ''
      return <section className={`plan-day ${date === today ? 'is-today' : ''} ${date < today ? 'is-past' : ''}`} key={date}>
        <h3>{date.slice(5).replace('-', ' / ')}{when && <em>{when}</em>}<span>{t('plan.item_count', { n: dayItems.length })}</span></h3>
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

// 三类来源的 chip 文案分开写：用户必须一眼看出这条是教材原文还是知识页转述。
function citationLabel(item: Citation): string {
  if (item.kind === 'web') return item.title || item.url || t('citation.web')
  if (item.kind === 'wiki') return t('citation.wiki_chip', { name: item.concept_name || item.concept_id || t('citation.wiki_fallback') })
  return `${item.material_name ?? t('citation.material_fallback')}${item.page ? `:${item.page}` : ''}`
}

function CitationChip({ item, fallbackNumber, onOpen }: { item: Citation; fallbackNumber: number; onOpen: (citation: Citation) => void }) {
  const label = <><i>[{item.number ?? fallbackNumber}]</i>{citationLabel(item)}</>
  const href = item.kind === 'web' ? safeHref(item.url) : null
  if (item.kind === 'wiki') return <button className="citation-wiki" onClick={() => onOpen(item)}>{label}</button>
  if (href) return <a className="citation-web" href={href} target="_blank" rel="noopener noreferrer nofollow" title={item.url}>{label}</a>
  return <button onClick={() => onOpen(item)}>{label}</button>
}

function MessageCard({ message, onCitation, showResolution, onRetry, modelNote, onChoose }: { message: Message; onCitation: (citation: Citation) => void; showResolution: boolean; onRetry?: () => void; modelNote?: string; onChoose?: (text: string) => void }) {
  if (message.role === 'user') return <article className="message user-message"><div>{message.content}</div></article>
  const isInterrupted = message.artifact?.kind === 'interrupted' || message.status === 'interrupted'
  // 课程会话的课程是固定的，逐条标注解析结果只会制造噪音；仅通用会话展示。
  const resolution = !showResolution ? null : message.resolution_status === 'resolved' ? t('message.resolved', { course: message.resolved_course_name ?? message.resolved_course_id ?? t('message.course_fallback') }) : message.resolution_status ? t('message.unresolved') : null
  return <article className="message assistant-message"><div className="agent-label"><span aria-hidden>❯</span><b>CoursePilot</b></div>{message.activity && message.activity.length > 0 && <ToolActivityRow activity={message.activity} />}
    {message.status === 'stopped' && <div className="degraded-notice"><span>{t('message.stopped_note')}</span>{onRetry && <button type="button" className="ghost-button" onClick={onRetry}>{t('message.retry')}</button>}</div>}
    {message.degraded && <div className="degraded-notice">{t('message.degraded_note', { note: message.degraded })}</div>}<div className={message.status === 'streaming' ? 'message-content streaming' : 'message-content'}>{message.content ? <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={markdownComponents}>{message.content}</ReactMarkdown> : <ThinkingHint activity={message.activity} modelNote={modelNote} />}</div>{resolution && <span className={`message-resolution ${message.resolution_status === 'resolved' ? 'resolved' : ''}`}>{resolution}</span>}{isInterrupted && <div className="interrupted"><span>{t('message.interrupted')}</span>{onRetry && <button type="button" className="ghost-button" onClick={onRetry}>{t('message.retry')}</button>}</div>}{message.citations && message.citations.length > 0 && <div className="citations"><span className="refs-label">SOURCES · {message.citations.length}</span>{message.citations.map((item, index) => <CitationChip key={`${item.id ?? item.chunk_id ?? item.url ?? index}`} item={item} fallbackNumber={index + 1} onOpen={onCitation} />)}</div>}{message.artifact && message.artifact.visibility !== 'model_private' && message.artifact.kind !== 'interrupted' && <div className="artifact-card"><b>{t('message.artifact_public')}</b><span>{message.artifact.kind}</span></div>}{message.choices && message.choices.length > 0 && onChoose && <div className="choices">{message.choices.map(option => <button type="button" className="choice" key={option} onClick={() => onChoose(option)}>{option}</button>)}</div>}</article>
}

function LibraryView({ course, onCourseChange, onError }: { course: Course; onCourseChange: (course: Course) => void; onError: (message: string) => void }) {
  const [tab, setTab] = useState<'rag' | 'concepts' | 'wiki' | 'notes'>('rag'); const [materials, setMaterials] = useState<Material[]>([]); const [jobs, setJobs] = useState<Record<string, Job>>({}); const [searchQuery, setSearchQuery] = useState(''); const [results, setResults] = useState<SearchResult[]>([]); const [searched, setSearched] = useState(''); const [loading, setLoading] = useState(false); const fileInput = useRef<HTMLInputElement>(null)
  const [ragBackend, setRagBackend] = useState<string>('')
  const polling = useRef(false)
  const [ocrTarget, setOcrTarget] = useState<string>(''); const [ocrEstimate, setOcrEstimate] = useState<OcrEstimate | null>(null); const [ocrRunning, setOcrRunning] = useState(false)
  const [wikiEstimates, setWikiEstimates] = useState<Record<string, WikiEstimate>>({})
  // 单独重算过目录结构：概念目录与结构面板都要跟着刷新
  const [structureRuns, setStructureRuns] = useState(0)
  const reload = async () => { try { setMaterials(await api.materials(course.id)) } catch (error) { onError(errorText(error)) } }
  const removeMaterial = async (materialId: string) => {
    try { await api.deleteMaterial(materialId); await reload() }
    catch (error) { onError(errorText(error)) }
  }
  const indexedMaterials = materials.filter(item => (item.index_status ?? item.status) === 'indexed')
  const indexedIds = indexedMaterials.map(item => item.id).join(',')
  useEffect(() => { api.health().then(payload => setRagBackend(((payload.rag as Record<string, unknown>)?.backend as string) ?? '')).catch(() => {}) }, [])
  useEffect(() => { setMaterials([]); setJobs({}); setResults([]); setSearched(''); void reload() }, [course.id])
  // 只轮询未终态的 job：jobs 只增不删，把已完成的也一起问一遍是白跑。
  // polling 挡重入——一轮超过 1.5s 时两轮会重叠，慢的那轮回来会覆盖掉新结果。
  useEffect(() => {
    const active = Object.values(jobs).filter(job => job.status === 'queued' || job.status === 'running')
    if (!active.length) return
    const interval = window.setInterval(() => {
      if (polling.current) return
      polling.current = true
      // 看门狗：请求永不 settle（代理挂住、休眠唤醒）时 finally 不会执行，
      // 锁就永久卡住、轮询彻底停摆。到点无条件放锁，最坏是多跑一轮。
      const unlock = window.setTimeout(() => { polling.current = false }, 10_000)
      void (async () => {
        try {
          const fresh = await Promise.all(active.map(job => api.job(job.id)))
          // 合并而非整表替换：这一轮等待期间用户可能又发起了新的索引任务。
          setJobs(current => ({ ...current, ...Object.fromEntries(fresh.map(job => [job.id, job])) }))
          await reload()
        } catch (error) { onError(errorText(error)) }
        finally { window.clearTimeout(unlock); polling.current = false }
      })()
    }, 1500)
    return () => window.clearInterval(interval)
  }, [jobs])
  async function upload(event: ChangeEvent<HTMLInputElement>) { const file = event.target.files?.[0]; if (!file) return; if (file.size > MAX_MATERIAL_BYTES) { onError(t('library.too_large')); return } setLoading(true); try { const material = await api.uploadMaterial(course.id, file); setMaterials(current => [material, ...current]); const job = await api.indexMaterial(material.id); setJobs(current => ({ ...current, [job.id]: job })) } catch (error) { onError(errorText(error)) } finally { setLoading(false); event.target.value = '' } }
  async function toggleWiki() { try { onCourseChange(await api.updateCourse(course.id, { wiki_enabled: !course.wiki_enabled })) } catch (error) { onError(errorText(error)) } }
  async function reindex(materialId: string) { try { const job = await api.indexMaterial(materialId); setJobs(current => ({ ...current, [job.id]: job })) } catch (error) { onError(errorText(error)) } }
  function askOcr(materialId: string) {
    // 估算本身要真跑两页，所以点开对话框就发起，拿到再显示数字
    setOcrTarget(materialId); setOcrEstimate(null)
    api.estimateOcr(materialId).then(setOcrEstimate).catch(error => { onError(errorText(error)); setOcrTarget('') })
  }
  async function confirmOcr() {
    if (!ocrTarget) return
    setOcrRunning(true)
    try { const job = await api.startOcr(ocrTarget); setJobs(current => ({ ...current, [job.id]: job })); setOcrTarget('') }
    catch (error) { onError(errorText(error)) } finally { setOcrRunning(false) }
  }
  async function buildWiki(materialId: string) { try { const job = await api.buildWiki(materialId); setJobs(current => ({ ...current, [job.id]: job })) } catch (error) { onError(errorText(error)) } }
  // Wiki 页列表要在构建结束后重新拉一次，否则新写的页要刷新整页才看得到
  const wikiDone = Object.values(jobs).filter(job => job.type === 'wiki' && job.status === 'completed').length
  // 概念目录同理，跟着索引任务的完成数刷新；单独重算过结构也要刷新
  const indexDone = Object.values(jobs).filter(job => job.type !== 'wiki' && job.status === 'completed').length
  // 账单只在教材内容变过之后作废：它按已落库的正文与概念目录算，构建知识页不影响它。
  useEffect(() => { setWikiEstimates({}) }, [course.id, indexDone, structureRuns])
  // 离线算的，不花额度，但每份教材都要全量读一遍正文。已经估过的不再重估，
  // 依赖用 id 列表而不是条数——同一轮里删掉一份、另一份索引完时长度不变。
  useEffect(() => {
    if (tab !== 'wiki' || !course.wiki_enabled) return
    const missing = indexedMaterials.filter(item => !wikiEstimates[item.id])
    if (!missing.length) return
    let cancelled = false
    void (async () => {
      const pairs = await Promise.all(missing.map(async item => {
        try { return [item.id, await api.estimateWiki(item.id)] as const } catch { return null }
      }))
      const fresh = pairs.filter(Boolean) as [string, WikiEstimate][]
      // 一条都没拿到就别写 state：写了会换掉对象身份，这个 effect 会被自己叫醒，转成死循环。
      if (!cancelled && fresh.length) setWikiEstimates(current => ({ ...current, ...Object.fromEntries(fresh) }))
    })()
    return () => { cancelled = true }
  }, [tab, course.id, course.wiki_enabled, indexedIds, wikiEstimates])
  async function search(event: FormEvent) { event.preventDefault(); if (!searchQuery.trim()) return; setLoading(true); try { setResults(await api.search(course.id, searchQuery)); setSearched(searchQuery) } catch (error) { onError(errorText(error)); setResults([]); setSearched('') } finally { setLoading(false) } }
  const backendLabel = retrievalLabel(ragBackend, true)
  return <section className="page"><div className="page-inner"><div className="hero"><div><p className="eyebrow">{t('nav.library')}</p><h1 className="course-heading"><i style={{ backgroundColor: course.color }} />{course.name}</h1><p>{t('library.hero')}{backendLabel && <span className="backend-badge">{backendLabel}</span>}</p></div><div className="hero-actions"><button className="ghost-button" onClick={() => void reload()}>{t('library.refresh_status')}</button></div></div><div className="tabs"><button className={tab === 'rag' ? 'active' : ''} onClick={() => setTab('rag')}>{t('library.tab_rag')}</button><button className={tab === 'concepts' ? 'active' : ''} onClick={() => setTab('concepts')}>{t('library.tab_concepts')}</button><button className={tab === 'wiki' ? 'active' : ''} onClick={() => setTab('wiki')}>{t('library.tab_wiki')} {course.wiki_enabled ? '' : t('library.tab_wiki_off')}</button><button className={tab === 'notes' ? 'active' : ''} onClick={() => setTab('notes')}>{t('library.notes_title')}</button></div>
    {tab === 'notes' && <NotesPanel course={course} onError={onError} />}
    {tab === 'concepts' && <>
      <StructurePanel course={course} refreshKey={indexDone + structureRuns} onError={onError} onParsed={() => setStructureRuns(runs => runs + 1)} />
      <ConceptTreePanel course={course} refreshKey={indexDone + structureRuns} onError={onError} />
    </>}
    {tab === 'rag' ? <><div className="library-grid"><article className="card upload-card"><h2>{t('library.upload_title')}</h2><p>{t('library.upload_body')}</p><p className="upload-hint">{t('library.upload_ocr_hint')}</p><input ref={fileInput} type="file" accept=".pdf,.txt,.md,.docx,.doc,.pptx,.ppt,text/plain,application/pdf,text/markdown" onChange={upload} hidden /><button className="primary-button" onClick={() => fileInput.current?.click()} disabled={loading}>{t('library.upload_button', { name: course.name })}</button><small>{t('library.upload_limits')}</small></article><article className="card search-card"><h2>{t('library.search_title')}</h2><p>{t('library.search_body', { name: course.name })}</p><form onSubmit={search}><input value={searchQuery} onChange={event => setSearchQuery(event.target.value)} placeholder={t('library.search_placeholder')} /><button className="primary-button" disabled={loading}>{t('library.search_button')}</button></form></article></div><article className="card material-card"><div className="card-heading"><div><h2>{t('library.materials_title')}</h2><p>{t('library.materials_hint')}</p></div><button className="text-button" onClick={() => void reload()}>{t('common.refresh')}</button></div>{materials.length ? materials.map(material => <MaterialRow material={material} jobs={jobs} key={material.id} onReindex={reindex} onDelete={removeMaterial} onOcr={askOcr} />) : <div className="empty-inline">{t('library.materials_empty')}</div>}</article>{searched && <article className="card results-card"><h2>{t('library.results_title')}</h2>{results.length === 0 && <div className="empty-inline"><b>{t('library.results_empty_title')}</b><p>{t('library.results_empty_body', { query: searched })}</p></div>}{results.map((result, index) => <div className="result" key={result.id ?? result.chunk_id ?? index}><b>{result.material_name ?? t('library.result_fallback_name')} {result.page ? `· p.${result.page}` : ''}</b><p>{result.text ?? t('library.result_no_text')}</p><small>{result.score !== undefined ? t('library.result_score', { score: result.score.toFixed(4) }) : t('library.result_cited')}</small></div>)}</article>}{ocrTarget && <OcrEstimatePanel filename={materials.find(item => item.id === ocrTarget)?.filename ?? t('library.ocr_fallback_name')} estimate={ocrEstimate} running={ocrRunning} onConfirm={() => void confirmOcr()} onCancel={() => setOcrTarget('')} />}</> : tab !== 'wiki' ? null : <><article className="card wiki-card"><div className="switch-row"><div><h2>{t('library.wiki_enable_title')} <span>{t('library.experimental')}</span></h2><p>{t('library.wiki_toggle_body')}</p></div><button className={`switch ${course.wiki_enabled ? 'on' : ''}`} aria-label={t('a11y.toggle_wiki')} onClick={toggleWiki}><i /></button></div>{course.wiki_enabled ? <><p className="wiki-note">{t('library.wiki_pick_hint')}</p>{indexedMaterials.length ? indexedMaterials.map(material => {
      // 取最后一个：jobs 只增不删，重建过的话前面那条是上次的 completed。
      const wikiJob = Object.values(jobs).filter(item => item.material_id === material.id && item.type === 'wiki').at(-1)
      const running = wikiJob ? !['completed', 'failed'].includes(wikiJob.status) : false
      return <div className="material-row" key={material.id}><div className="file-mark">{fileKind(material)}</div><div className="material-copy"><b>{material.filename ?? material.name ?? t('library.material_untitled')}</b><small>{wikiJob ? stageLabel(wikiJob.stage ?? wikiJob.status, String(wikiJob.status)) : t('library.wiki_ready')}</small>{!running && <WikiEstimateNote estimate={wikiEstimates[material.id]} />}{wikiJob && <div className="job-progress"><i style={{ width: `${wikiJob.progress ?? 15}%` }} /></div>}{wikiJob?.error && <WikiBuildNote job={wikiJob} />}</div><button className="ghost-button" onClick={() => void buildWiki(material.id)} disabled={running}>{wikiJob && !running ? t('library.wiki_rebuild') : t('library.wiki_build')}</button></div>
    }) : <div className="empty-inline">{t('library.wiki_needs_material')}</div>}</> : <div className="empty-inline"><b>{t('library.wiki_off_title')}</b><p>{t('library.wiki_off_body')}</p></div>}</article>{course.wiki_enabled && <WikiPagesPanel course={course} refreshKey={wikiDone} onError={onError} />}</>}</div></section>
}

/** 构建前的账单。页数与调用次数是离线算的，分钟数按每页约 5 秒外推。 */
function WikiEstimateNote({ estimate }: { estimate?: WikiEstimate }) {
  if (!estimate) return <small className="wiki-coverage">{t('library.wiki_estimating')}</small>
  return <small className="wiki-coverage">
    {t('library.wiki_estimate', { pages: estimate.pages, calls: estimate.calls, minutes: estimate.minutes })}
    {estimate.merged > 0 && ` ${t('library.wiki_estimate_merged', { n: estimate.merged })}`}
    {!estimate.has_levels && ` ${t('library.wiki_estimate_flat')}`}
  </small>
}

/** Wiki 构建的收尾提示。成功时后端给的是覆盖率字段串，按语言渲染；失败时原样显示报错。 */
function WikiBuildNote({ job }: { job: Job }) {
  const raw = job.error ?? ''
  if (!raw.startsWith('wiki_coverage ')) return <small className="danger-text">{raw}</small>
  const fields: Record<string, number> = {}
  for (const item of raw.split(' ').slice(1)) {
    const [key, value] = item.split('=')
    fields[key] = Number(value) || 0
  }
  return <small className="wiki-coverage">
    {t('library.wiki_coverage', { concepts: fields.concepts, pages: fields.pages })}
    {fields.merged > 0 && ` ${t('library.wiki_coverage_merged', { merged: fields.merged })}`}
    {' '}{t('library.wiki_coverage_detail', { written: fields.written, skipped: fields.skipped })}
  </small>
}

/** 后端把进度写成 `wiki 12/51`、`ocr 3/10`：拆出数字渲染成正常文案，别把原始串摆给用户。 */
function stageLabel(stage: string | undefined, fallback: string): string {
  const progress = /^(wiki|ocr) (\d+)\/(\d+)$/.exec(stage ?? '')
  if (progress) {
    return t(progress[1] === 'wiki' ? 'stage.wiki_progress' : 'stage.ocr_progress',
             { done: progress[2], total: progress[3] })
  }
  return tOr(`stage.${String(stage ?? fallback)}`, fallback)
}

// 阶段名在字典里（`stage.<name>` 与 `pipeline.<name>`）：后端加新阶段时界面显示原始值。
// 前四步是检索索引，最后一步是目录结构——两条流水线，共享前面的文本准备。
const INDEX_PIPELINE = ['extracting', 'chunking', 'embedding', 'indexing', 'structure'] as const

function MaterialRow({ material, jobs, onReindex, onDelete, onOcr }: { material: Material; jobs: Record<string, Job>; onReindex: (materialId: string) => void; onDelete?: (materialId: string) => Promise<void>; onOcr?: (materialId: string) => void }) {
  const [confirming, setConfirming] = useState(false)
  // 只看索引任务，且取最后一个：jobs 只增不删，wiki 任务与上一次重建都在里面。
  const job = Object.values(jobs).filter(item => item.material_id === material.id && item.type !== 'wiki').at(-1)
  const rawStatus = job?.stage ?? job?.status ?? material.index_status ?? material.status ?? 'uploaded'
  const statusLabel = stageLabel(String(rawStatus), String(rawStatus))
  const failed = String(job?.status ?? rawStatus).toLowerCase().includes('fail')
  const jobActive = job ? !['completed', 'failed'].includes(job.status) : false
  const indexed = (material.index_status ?? material.status) === 'indexed'
  const semantic = (material.embedded_count ?? 0) > 0
  const stageIndex = INDEX_PIPELINE.findIndex(stage => stage === job?.stage)
  // 扫描版停在这里等确认：OCR 要花模型额度，先给账单再让用户点
  const needsOcr = job?.stage === 'needs_ocr' || (material.index_status ?? material.status) === 'needs_ocr'
  const productSummary = indexed && !jobActive
    ? t(semantic ? 'library.product_semantic' : 'library.product_lexical', { n: material.chunk_count ?? 0 })
    : null
  return <div className="material-row">
    <div className="file-mark">{fileKind(material)}</div>
    <div className="material-copy">
      <b>{material.filename ?? material.name ?? t('library.material_untitled')}</b>
      <small>{[material.size_bytes ? `${Math.ceil(material.size_bytes / 1024 / 1024)} MiB` : null, productSummary].filter(Boolean).join(' · ') || statusLabel}</small>
      {jobActive && <div className="pipeline">{INDEX_PIPELINE.map((stage, position) => <span key={stage} className={`pipeline-step ${stageIndex > position ? 'done' : stageIndex === position ? 'current' : ''}`}>{t(`pipeline.${stage}`)}</span>)}</div>}
      {job && !failed && <div className="job-progress"><i style={{ width: `${job.progress ?? 15}%` }} /></div>}
      {failed && job?.error && <small className="danger-text">{job.error}</small>}
    </div>
    {needsOcr && onOcr && <button className="primary-button" onClick={() => onOcr(material.id)}>{t('library.ocr_estimate_button')}</button>}
    {!jobActive && !needsOcr && <button className="text-button" onClick={() => onReindex(material.id)}>{failed ? t('library.reindex_retry') : t('library.reindex')}</button>}
    {!jobActive && onDelete && <button className="text-button danger-text" onClick={() => setConfirming(true)}>{t('common.delete')}</button>}
    <span className={`status-tag ${failed ? 'failed' : ''}`}>{statusLabel}</span>
    {confirming && onDelete && <DangerConfirm
      what={t('library.delete_material_what', { name: material.filename ?? material.name ?? t('library.material_untitled') })}
      consequences={[
        t('library.delete_material.c1'),
        t('library.delete_material.c2'),
        t('library.delete_material.c3'),
      ]}
      onConfirm={() => { setConfirming(false); void onDelete(material.id) }}
      onCancel={() => setConfirming(false)} />}
  </div>
}
function fileKind(material: Material) { const name = material.filename ?? material.name ?? ''; return name.split('.').pop()?.toUpperCase().slice(0, 4) || 'FILE' }

const thousands = (value: number) => value.toLocaleString('en-US')
/** 几页教材的外推耗时不到一分钟，四舍五入会显示成「约 0 分钟」。 */
const duration = (seconds: number | undefined, minutes: number) =>
  seconds !== undefined && seconds < 60 ? t('ocr.seconds', { n: Math.max(1, seconds) }) : t('ocr.minutes', { n: minutes })

/** OCR 账单。取样那行是真跑出来的，全书那行是按页数外推的——两者要分开写，
 *  否则会让人以为外推值也是实测。 */
function OcrEstimatePanel({ filename, estimate, running, onConfirm, onCancel }: {
  filename: string; estimate: OcrEstimate | null; running: boolean
  onConfirm: () => void; onCancel: () => void
}) {
  return <article className="card ocr-card">
    <h2>{t('ocr.title', { name: filename })}</h2>
    <p>{t('ocr.body')}</p>
    {estimate === null ? <p className="mini-empty">{t('ocr.estimating')}</p> : <table className="ocr-estimate"><tbody>
      <tr><th>{t('ocr.pages')}</th><td>{t('ocr.pages_value', { n: estimate.pages })}</td></tr>
      <tr><th>{t('ocr.sampled')}</th><td>{t('ocr.sample_value', { pages: estimate.sampled_pages, tokens: thousands(estimate.sample_prompt_tokens + estimate.sample_completion_tokens), seconds: estimate.sample_seconds })}</td></tr>
      <tr><th>{t('ocr.projected')}</th><td><b>{t('ocr.tokens', { n: thousands(estimate.projected_total_tokens) })}</b>{t('ocr.projected_split', { prompt: thousands(estimate.projected_prompt_tokens), completion: thousands(estimate.projected_completion_tokens) })}</td></tr>
      <tr><th>{t('ocr.eta')}</th><td>{duration(estimate.projected_seconds, estimate.projected_minutes)}</td></tr>
    </tbody></table>}
    <small className="help-note">{t('ocr.note')}</small>
    <div className="danger-actions">
      <button className="primary-button" disabled={estimate === null || running} onClick={onConfirm}>
        {running ? t('ocr.running') : t('ocr.confirm')}
      </button>
      <button className="ghost-button" onClick={onCancel}>{t('common.cancel')}</button>
    </div>
  </article>
}

function PlanView({ course, onError }: { course: Course; onError: (message: string) => void }) {
  const [plan, setPlan] = useState<Plan | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')
  // 重试计数进依赖，否则「重新读取」不会重新发请求，界面永远停在 loading。
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    setPlan(null); setLoaded(false); setError('')
    api.plan(course.id).then(payload => setPlan(payload.plan)).catch(error => { setError(errorText(error)); onError(errorText(error)) }).finally(() => setLoaded(true))
  }, [course.id, attempt])
  return <section className="page"><div className="page-inner">
    <div className="hero"><div><p className="eyebrow">{t('nav.plan')}</p><h1 className="course-heading"><i style={{ backgroundColor: course.color }} />{course.name}</h1><p>{t('plan.hero')}</p></div></div>
    {!loaded ? <p className="mini-empty">{t('plan.loading')}</p> : error ? <RetryCard title={t('plan.error_title')} message={error} onRetry={() => setAttempt(n => n + 1)} /> : plan ? <article className="card"><div className="card-heading"><div><h2>{t('plan.current_title')}</h2><p>{t('plan.meta', { version: plan.version, n: plan.items.length, time: plan.updated_at.slice(0, 16).replace('T', ' ') })}</p></div></div><PlanGantt items={plan.items} /><PlanDays items={plan.items} /></article> : <article className="card"><h2>{t('plan.empty_title')}</h2><p>{t('plan.empty_body')}</p></article>}
  </div></section>
}
function ArchiveView({ course, onError }: { course: Course; onError: (message: string) => void }) {
  const [archive, setArchive] = useState<ArchiveSummary | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    setArchive(null); setLoaded(false); setError('')
    api.archive(course.id).then(setArchive).catch(error => { setError(errorText(error)); onError(errorText(error)) }).finally(() => setLoaded(true))
  }, [course.id, attempt])
  return <section className="page"><div className="page-inner">
    <div className="hero"><div><p className="eyebrow">{t('nav.archive')}</p><h1 className="course-heading"><i style={{ backgroundColor: course.color }} />{course.name}</h1><p>{t('archive.hero')}</p></div></div>
    {!loaded ? <p className="mini-empty">{t('archive.loading')}</p> : error ? <RetryCard title={t('archive.error_title')} message={error} onRetry={() => setAttempt(n => n + 1)} /> : !archive ? <p className="mini-empty">{t('archive.empty')}</p> : <>
      <article className="card"><div className="card-heading"><div><h2>{t('archive.mastery_title')}</h2><p>{t('archive.mastery_hint')}</p></div></div>
        {archive.mastery.length ? archive.mastery.map(item => <div className="material-row" key={item.concept_id}>
          <div className="file-mark">{item.insufficient_evidence ? '—' : `${Math.round((item.score ?? 0) * 100)}`}</div>
          <div className="material-copy"><b>{item.name}</b>
            <small>{t(item.insufficient_evidence ? 'archive.insufficient' : 'archive.evidence_count', { n: item.objective_events })}{item.due_at ? ` · ${t('archive.due', { date: item.due_at.slice(0, 10) })}` : ''}</small>
            {!item.insufficient_evidence && <div className="job-progress"><i style={{ width: `${Math.round((item.score ?? 0) * 100)}%` }} /></div>}
          </div>
        </div>) : <div className="empty-inline">{t('archive.mastery_empty')}</div>}
      </article>
      <MistakesCard archive={archive} />
      <article className="card"><div className="card-heading"><div><h2>{t('archive.events_title')}</h2><p>{t('archive.events_count', { n: archive.evidence_count })}</p></div></div>{archive.events.length ? archive.events.map(event => <div className="material-row" key={event.id}><div className="file-mark">{event.kind.toUpperCase().slice(0, 4)}</div><div className="material-copy"><b>{event.concept_name ?? event.topic_hint ?? (event.concept_id ? t('archive.attributed') : t('archive.unattributed'))}</b><small>{event.attribution_status} · {timeLabel(event.created_at)}</small></div></div>) : <div className="empty-inline">{t('archive.events_empty')}</div>}</article>
      {archive.unattributed.length > 0 && <article className="card"><div className="card-heading"><div><h2>{t('archive.topics_title')}</h2><p>{t('archive.topics_hint')}</p></div></div>
        {archive.unattributed.map(item => <div className="material-row" key={item.topic_hint}><div className="file-mark">{item.hits}</div><div className="material-copy"><b>{item.topic_hint}</b><small>{t('archive.last_seen', { time: timeLabel(item.last_seen) })}</small></div></div>)}
      </article>}
    </>}
  </div></section>
}

function MistakeRow({ record, goal }: { record: MistakeRecord; goal: number }) {
  const done = Math.min(record.streak, goal)
  return <div className="material-row">
    <div className="file-mark" title={t('archive.mistakes_wrong_total', { n: record.wrong_count })}>{record.wrong_count}</div>
    <div className="material-copy"><b>{record.name}</b>
      <small>{t('archive.mistakes_wrong', { n: record.wrong_count })} · {t('archive.mistakes_streak', { done, goal })} · {t('archive.mistakes_last_wrong', { time: timeLabel(record.last_wrong_at) })}</small>
      <div className="job-progress"><i style={{ width: `${goal ? Math.round((done / goal) * 100) : 0}%` }} /></div>
    </div>
    <span className="mistake-tags">
      {record.relapse_count > 0 && <span className="status-tag failed">{record.relapse_count > 1 ? t('archive.mistakes_relapse_n', { n: record.relapse_count }) : t('archive.mistakes_relapse')}</span>}
      {record.status === 'graduated' && <span className="status-tag">{t('archive.mistakes_cleared_tag')}</span>}
    </span>
  </div>
}

/** 错题本。`mistakes` 只是一页，所以截断要显式说出来；毕业的折叠起来但计数始终可见——
 *  清掉的东西看得见，用户才知道这套机制在起作用。 */
function MistakesCard({ archive }: { archive: ArchiveSummary }) {
  // 阈值跟着响应走：前端再存一份常量，后端改了界面就会理直气壮地说假话。
  const goal = archive.graduate_streak
  const active = archive.mistakes.filter(item => item.status === 'active')
  const graduated = archive.mistakes.filter(item => item.status === 'graduated')
  const hidden = Math.max(archive.active_count - active.length, 0)
  return <article className="card"><div className="card-heading"><div><h2>{t('archive.mistakes_title')}</h2><p>{t('archive.mistakes_hint', { n: goal })}</p></div></div>
    {archive.active_count + archive.graduated_count === 0
      ? <div className="empty-inline">{t('archive.mistakes_empty', { n: goal })}</div>
      : <>
        {active.length ? active.map(item => <MistakeRow key={item.concept_id} record={item} goal={goal} />) : <div className="empty-inline">{t('archive.mistakes_all_clear')}</div>}
        {hidden > 0 && <p className="mistake-more">{t('archive.mistakes_more', { n: hidden })}</p>}
        {archive.graduated_count > 0 && <details className="mistake-cleared">
          <summary>{t('archive.mistakes_cleared', { n: archive.graduated_count })}</summary>
          {graduated.length ? graduated.map(item => <MistakeRow key={item.concept_id} record={item} goal={goal} />) : <div className="empty-inline">{t('archive.mistakes_cleared_hidden')}</div>}
        </details>}
      </>}
  </article>
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
  return <article className="card"><h2>{t('memory.title')}</h2>
    <p>{t('memory.body1')}<code>user.md</code>{t('memory.body2')}<code>memory.md</code>{t('memory.body3')}</p>
    <div className="memory-head">
      <select value={scope} onChange={event => setScope(event.target.value)} aria-label={t('a11y.memory_scope')}>
        <option value="user">{t('memory.scope_user')}</option>
        {courses.map(course => <option value={course.id} key={course.id}>{t('memory.scope_course', { name: course.name })}</option>)}
      </select>
      <span>{dirty ? t('memory.dirty') : loaded ? t('memory.clean') : t('memory.loading')}</span>
      <button className="ghost-button" disabled={!dirty || saving} onClick={() => void save()}>{saving ? t('memory.saving') : t('common.save')}</button>
      <button className="ghost-button" disabled={!dirty} onClick={() => setDraft(content)}>{t('memory.discard')}</button>
    </div>
    <textarea className="memory-editor" value={draft} onChange={event => setDraft(event.target.value)}
      placeholder={loaded ? t('memory.placeholder') : t('memory.loading')}
      spellCheck={false} aria-label={t('a11y.memory_content')} />
    <p className="help-note">{t('memory.note1')}<code>agent:managed</code>{t('memory.note2')}</p>
  </article>
}

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
  const sep = t('common.list_sep')
  return <article className="card"><h2>{t('settings.skills_title')}</h2>
    <p>{t('settings.skills_hint', { tools: importable.join(sep) || '—' })}</p>
    {skills === null ? <p className="empty-inline">{t('common.loading')}</p> : skills.map(skill => <div className="skill-row" key={skill.name}>
      <div className="skill-copy">
        <b>{skill.name}<em>{skill.origin === 'builtin' ? t('skill.origin_builtin') : t('skill.origin_user')}</em></b>
        <small>{skill.when_to_use}</small>
        <small className="skill-tools">{t('settings.skill_tools', { tools: skill.allowed_tools.join(sep) || '—' })}</small>
        {skill.denied_tools.length > 0 && <small className="skill-denied">{t('settings.skill_denied', { tools: skill.denied_tools.join(sep) })}</small>}
      </div>
      <div className="skill-actions">
        <span className={`skill-status ${skill.status}`}>{tOr(`skill.status.${skill.status}`, skill.status)}</span>
        {skill.origin === 'user' && <>
          <button className="ghost-button" disabled={busy || skill.status === 'permission_denied'} onClick={() => void run(() => api.setSkillEnabled(skill.name, skill.status !== 'enabled'))}>{skill.status === 'enabled' ? t('settings.disable') : t('settings.enable')}</button>
          <button className="ghost-button danger" disabled={busy} onClick={() => void run(() => api.deleteSkill(skill.name))}>{t('common.delete')}</button>
        </>}
      </div>
    </div>)}
    <div className="skill-import">
      <button className="ghost-button" disabled={busy} onClick={() => fileInput.current?.click()}>{t('settings.import_file')}</button>
      <button className="ghost-button" disabled={busy} onClick={() => folderInput.current?.click()}>{t('settings.import_folder')}</button>
    </div>
    <small className="help-note">{t('settings.import_note')}</small>
    {skipped.length > 0 && <small className="skill-denied">{t('settings.skipped', { files: skipped.join(sep) })}</small>}
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
  return <section className="page"><div className="page-inner"><div className="hero"><div><h1>{t('nav.settings')}</h1><p>{t('settings.hero')}</p></div><button className="ghost-button" onClick={check} disabled={loading}>{t('settings.check')}</button></div><div className="settings-grid"><article className="card"><h2>{t('settings.courses_title')}</h2><p>{t('settings.courses_count', { n: courses.length })}</p>{courses.length ? courses.map(course => <CourseSettingRow key={course.id} course={course} onDelete={onCourseDeleted} onError={onError} />) : <p className="empty-inline">{t('settings.courses_empty')}</p>}</article><MemoryCard courses={courses} onError={onError} /><SkillsCard onError={onError} /><article className="card health-card"><h2>{t('settings.health_title')}</h2>{health ? <><dl>
    <div><dt>{t('help.fact_model')}</dt><dd>{llm ? `${String(llm.provider)} / ${String(llm.model)} · ${llm.enabled ? t('settings.remote_on') : t('settings.local_demo')}` : t('common.unknown')}</dd></div>
    <div><dt>{t('settings.fact_retrieval')}</dt><dd>{retrievalLabel(rag?.backend as string | undefined)}</dd></div>
    {embedding && <div><dt>{t('settings.fact_embedding')}</dt><dd>{String(embedding.model)} · {embedding.error ? t('settings.embed_failed', { error: String(embedding.error) }) : embedding.loaded ? t('settings.embed_loaded') : t('settings.embed_lazy')}</dd></div>}
    <div><dt>{t('settings.fact_db')}</dt><dd>{(health.database as Record<string, unknown>)?.ok ? t('settings.db_ok', { version: String((health.database as Record<string, unknown>)?.migration_version) }) : t('settings.db_bad')}</dd></div>
  </dl><details><summary>{t('settings.raw_json')}</summary><pre>{JSON.stringify(health, null, 2)}</pre></details></> : <p>{t('settings.health_hint')}</p>}</article></div></div></section>
}
function CoursePickerState({ view, courses, onPick, onCreate }: { view: View; courses: Course[]; onPick: (courseId: string) => void; onCreate: () => void }) {
  return <section className="page"><div className="page-inner empty-course"><span aria-hidden>❯</span><h1>{t('picker.title')}</h1><p>{t('picker.body', { view: viewName(view) })}</p>
    <div className="picker-grid">{courses.map(item => <button className="picker-card" key={item.id} onClick={() => onPick(item.id)}><i style={{ backgroundColor: item.color }} /><b>{item.name}</b>{item.wiki_enabled && <em>Wiki</em>}</button>)}<button className="picker-card picker-create" onClick={onCreate}>{t('course.new')}</button></div>
  </div></section>
}
/** 一页知识页依据的教材页，按文档归一成「文档 第 9–11 页」。列出的页可能被截断，
 *  所以两端由服务端保证准确，中间少几页只影响能点开哪几页。 */
function sourceSpans(sources: CitationSource[]): { document: string; pages: string; items: CitationSource[] }[] {
  const byDocument = new Map<string, CitationSource[]>()
  for (const item of sources) byDocument.set(item.document, [...(byDocument.get(item.document) ?? []), item])
  return [...byDocument].map(([document, items]) => {
    const numbers = items.map(item => item.page).filter((page): page is number => typeof page === 'number')
    const range = numbers.length === 0 ? '' : numbers[0] === numbers[numbers.length - 1]
      ? String(numbers[0]) : `${numbers[0]}–${numbers[numbers.length - 1]}`
    return { document, pages: range, items }
  })
}

/** 出处那一页点开后仍是教材原文的抽屉，只是它不占引用编号。 */
function asMaterial(source: CitationSource): Citation {
  return { kind: 'material', material_name: source.document, page: source.page, chunk_id: source.chunk_id, text: source.snippet }
}

function WikiSources({ citation, onOpen }: { citation: Citation; onOpen: (citation: Citation) => void }) {
  const sources = citation.sources ?? []
  if (sources.length === 0) return null
  const total = citation.source_pages ?? sources.length
  return <div className="citation-sources">
    <p className="citation-location">{t('citation.wiki_sources')}</p>
    {sourceSpans(sources).map(span => <div className="citation-span" key={span.document}>
      <b>{span.pages ? t('citation.wiki_source_span', { document: span.document, pages: span.pages }) : span.document}</b>
      <div>{span.items.map(item => <button type="button" key={`${item.document}:${item.page}`} onClick={() => onOpen(asMaterial(item))}>{t('citation.page_short', { n: item.page ?? 0 })}</button>)}</div>
    </div>)}
    {total > sources.length && <p className="citation-location">{t('citation.wiki_sources_more', { n: total, m: sources.length })}</p>}
  </div>
}

function CitationDrawer({ citation, onClose, onOpen }: { citation: Citation; onClose: () => void; onOpen: (citation: Citation) => void }) {
  const isWiki = citation.kind === 'wiki'
  // 抽屉头部就要说清这是转述稿：正文没有页码，用户不该以为自己在看教材原文。
  const heading = isWiki ? (citation.concept_name || citation.concept_id || t('citation.wiki_fallback')) : (citation.material_name ?? t('citation.fallback_name'))
  const location = isWiki ? t('citation.wiki_location')
    : citation.page ? t('citation.page', { n: citation.page })
    : citation.chunk_id ? t('citation.chunk', { id: citation.chunk_id }) : t('citation.location_unknown')
  return <aside className="citation-drawer" role="dialog" aria-label={t('a11y.citation_drawer')}><header><div><p>{isWiki ? t('citation.wiki_title') : t('citation.title')}</p><h2>{heading}</h2></div><button aria-label={t('a11y.close_citation')} onClick={onClose}>×</button></header><p className="citation-location">{location}</p><blockquote>{citation.text ?? t('citation.no_text')}</blockquote>{isWiki && <WikiSources citation={citation} onOpen={onOpen} />}{citation.score !== undefined && <p>{t('citation.score', { score: citation.score.toFixed(4) })}</p>}</aside>
}
