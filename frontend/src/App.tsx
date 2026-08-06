import { ChangeEvent, Children, ComponentProps, createContext, FormEvent, ReactElement, ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import remarkCjkFriendly from 'remark-cjk-friendly'
import rehypeKatex from 'rehype-katex'
import 'katex/dist/katex.min.css'
import { api, ApiError, clearCurrentUser, currentDevMode, currentModel, currentThinking, currentUser, onConnectionLost, setCurrentDevMode, setCurrentModel, setCurrentThinking, setCurrentUser } from './api'
import { getLang, LangContext, LANGS, locale, nameParts, setLang, t, tOr, useI18n, type Lang } from './i18n'
import type { ArchiveSummary, Attachment, Citation, CitationSource, ConceptNode, ContextUsage, Course, Job, Material, MaterialStructure, McpServer, Message, MistakeRecord, Plan, ScopeMode, SearchResult, NoteSummary, OcrEstimate, SessionTrace, SessionSummary, SkillInfo, StructurePreview, ToolActivity, TraceBody, TraceStep, TraceSubagent, TraceTool, TraceTurn, WikiEdge, WikiEstimate, WikiIssue, WikiPageSummary } from './types'

/** 开发者模式。关掉时 openTrace 为 null——入口靠这一个判断决定要不要渲染成可点的元素，
 *  免得出现「按钮还在、只是点了没反应」这种状态。 */
type DevMode = { enabled: boolean; setEnabled: (on: boolean) => void; openTrace: ((turnId: string) => void) | null }
const DevModeContext = createContext<DevMode>({ enabled: false, setEnabled: () => {}, openTrace: null })
function useDevMode() { return useContext(DevModeContext) }

type View = 'chat' | 'library' | 'plan' | 'archive' | 'settings' | 'help'
type Workspace = { scope: ScopeMode; courseId?: string }
type TurnResolution = { sessionId: string; status: string; courseId: string | null; courseName: string | null }

function viewName(view: View) { return t(`nav.${view}`) }
// 原来是 01–04 的编号。这四个页面不是一个序列，编号不编码任何真实信息，
// 纯装饰还占掉标识槽的宽度，换成线性图标。
const nav: { id: View; icon: IconName }[] = [
  { id: 'chat', icon: 'chat' }, { id: 'library', icon: 'shelf' }, { id: 'plan', icon: 'calendar' }, { id: 'archive', icon: 'flag' },
]

type IconName = 'compass' | 'chat' | 'shelf' | 'calendar' | 'flag'
/** 侧栏图标。统一 16px、round 端点，和方块标记的笔画语言一致。 */
function NavIcon({ name }: { name: IconName }) {
  const paths: Record<IconName, ReactElement> = {
    compass: <><circle cx="9" cy="9" r="6.8" /><path d="M11.8 6.2 7.9 7.9 6.2 11.8l3.9-1.7z" fill="currentColor" stroke="none" /></>,
    chat: <path d="M2.5 3.5h13v8H6l-3.5 3z" />,
    shelf: <path d="M2.5 3.5h4v11h-4zM8 3.5h3.5v11H8zM13 4.6l2.2.5-2 10.4-2.2-.5z" />,
    calendar: <><rect x="2.5" y="3.5" width="13" height="11.5" rx="1.6" /><path d="M2.5 7h13M6 2v3M12 2v3" /></>,
    flag: <path d="M3.5 2.5v13M3.5 3.5h10l-2 3.5 2 3.5h-10" />,
  }
  return <svg width="16" height="16" viewBox="0 0 18 18" fill="none" stroke="currentColor"
    strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden focusable="false">{paths[name]}</svg>
}
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
  artifact_append: 'write_state', mcp_propose: 'write_state', note_write: 'write_note',
  web_search: 'network', web_fetch: 'network',
  use_skill: 'free', artifact_read: 'free', calculator: 'free', ask_user: 'free',
  delegate: 'delegate',
}

/** 浮层的关闭手势：Esc，以及在浮层外面点一下。返回的 ref 要挂到浮层根节点上。
 *
 *  不用全屏遮罩：抽屉是非模态的，开着的时候正文要能继续滚动、选中，SOURCES 那排
 *  引用 chip 也要能一次点击就换到下一条。
 *  拖选正文时鼠标也落在浮层外面，所以按下不立即关，等松开时确认没拖动、也没新产生选中。
 *  已知边界：双击/三击选词仍会关——第一次 click 松手时词还没选上，那一刻和普通点击无法区分。
 *  要挡住得把关闭延后 ~250ms 等后续点击，代价是每次关浮层都变迟钝，不值。 */
function useDismiss(onClose: () => void) {
  const latest = useRef(onClose)
  useEffect(() => { latest.current = onClose })
  const box = useRef<HTMLElement | null>(null)
  useEffect(() => {
    let waiting: ((event: MouseEvent) => void) | null = null
    function outside(target: EventTarget | null) {
      return box.current !== null && target instanceof Node && !box.current.contains(target)
    }
    // 输入法里 Esc 是收候选框，不该连带关掉浮层（composer 挡 Enter 用的是同一个判断）
    function onKey(event: KeyboardEvent) { if (event.key === 'Escape' && !event.isComposing) latest.current() }
    function onDown(event: MouseEvent) {
      // 只认左键：右键唤出的是上下文菜单，不是「点了外面」。右键点空白或图片时
      // 下面那个选中比较挡不住（选中没变化），所以这个守卫是承重的。
      if (event.button !== 0 || !outside(event.target)) return
      // 上一次手势的 mouseup 可能被 dragstart 吞掉（拖链接、拖图片），监听器会留在
      // document 上。不在这里清掉的话，它会用当初的坐标把后面每一次点击都算成拖动，
      // 「点外面关闭」从此彻底失效。
      if (waiting) { document.removeEventListener('mouseup', waiting); waiting = null }
      const { clientX, clientY } = event
      // 只在这一次手势「新产生」选中时才不关。不能比「选中变没变」——点在可选中区域上
      // 会清掉旧选中，那也算变了，于是残留选中时点正文要点两次才关得掉。
      const before = String(window.getSelection() ?? '')
      const settle = (up: MouseEvent) => {
        waiting = null
        const dragged = Math.abs(up.clientX - clientX) > 4 || Math.abs(up.clientY - clientY) > 4
        const now = String(window.getSelection() ?? '')
        if (!dragged && !(now && now !== before)) latest.current()
      }
      waiting = settle
      document.addEventListener('mouseup', settle, { once: true })
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onDown)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onDown)
      if (waiting) document.removeEventListener('mouseup', waiting)
    }
  }, [])
  return box
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

/** 方块标记 + 字标。两处用（登录页与侧栏），所以抽出来。
 *  字标只做两处排版微调：字距收到 -0.022em、Course 450 / Pilot 680 的双字重。
 *  字形仍是系统字体——手绘 SVG 字形那版作者判「太花哨」，收回了。 */
function Brand({ size = 28 }: { size?: number }) {
  return <div className="brand">
    <svg className="brandmark" width={size} height={size} viewBox="0 0 60 60" aria-hidden focusable="false">
      <rect width="60" height="60" rx="14" fill="var(--text)" />
      <path d="M20 20h13l7 7v13" fill="none" stroke="var(--bg)" strokeWidth="4.2" strokeLinecap="round" strokeLinejoin="round" />
      <rect x="20" y="36.2" width="13" height="4.4" rx="2.2" fill="var(--accent)" />
    </svg>
    <div className="brand-copy">
      <strong><span>Course</span><b>Pilot</b></strong>
    </div>
  </div>
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
      <Brand />
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
  // 每个会话最后一次被看过的时间，只落在本地——未读是本机的阅读状态，不是服务端数据
  const [seen, setSeen] = useState<Record<string, string>>(() => readSeen())
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => localStorage.getItem('cp-sidebar-collapsed') === 'true')
  // 生成回答与新建课程/会话分开：一次回答要跑一分钟，这一分钟里不该连侧栏都点不动。
  const [streaming, setStreaming] = useState(false)
  const [creating, setCreating] = useState(false)
  const [notice, setNotice] = useState('')
  const [apiOnline, setApiOnline] = useState<boolean | null>(null)
  const [health, setHealth] = useState<Record<string, unknown> | null>(null)
  const [citation, setCitation] = useState<Citation | null>(null)
  const [devMode, setDevMode] = useState(() => currentDevMode())
  // 侧栏只留一个位置：trace 与引用抽屉都是右侧 fixed，同时开会叠在一起。
  const [traceTurn, setTraceTurn] = useState<string | null>(null)
  const [turnResolution, setTurnResolution] = useState<TurnResolution | null>(null)
  // 上下文构成来自服务端实际组装结果；换会话就清空，避免显示上一会话的数字。
  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(null)
  // 屏幕上正在显示的那个会话就算看过了——包括启动时自动选中的那个。不记的话，
  // 切走再切回它又变成未读。等 streaming 收尾再记：那一轮把 updated_at 推到了
  // 上次记录之后，而用户是看着它跑完的。
  const activeId = activeSession?.id
  useEffect(() => {
    if (!activeId || streaming) return
    markSeen(activeId)
    setSeen(readSeen())
  }, [activeId, streaming])
  // 帮助页点例句后带进对话输入框
  const [draftSeed, setDraftSeed] = useState('')
  // 停止生成：中断 SSE 读取，服务端 finally 会把这一轮落成终态，已生成的内容仍在库里
  const abortRef = useRef<AbortController | null>(null)
  // reader.cancel() 会让读取正常结束而不抛错，所以"是否被停止"要显式记，不能靠捕获异常判断。
  const stoppedRef = useRef(false)

  const i18n = useMemo(() => ({ lang, setLang: (next: Lang) => { setLang(next); setLangState(next) } }), [lang])
  const openTrace = useCallback((turnId: string) => { setCitation(null); setTraceTurn(turnId) }, [])
  const dev = useMemo<DevMode>(() => ({
    enabled: devMode,
    setEnabled: (on: boolean) => { setCurrentDevMode(on); setDevMode(on); if (!on) setTraceTurn(null) },
    openTrace: devMode ? openTrace : null,
  }), [devMode, openTrace])
  const course = useMemo(() => courses.find(item => item.id === workspace.courseId) ?? null, [courses, workspace.courseId])
  const heading = activeSession?.title && view === 'chat' ? activeSession.title : viewName(view)

  useEffect(() => { localStorage.setItem('cp-sidebar-collapsed', String(sidebarCollapsed)) }, [sidebarCollapsed])
  // 换会话就收掉 trace 侧栏：轮次 id 属于上一个会话，留着只会显示成「这一轮没有记录」。
  // 写成 effect，逐个入口清的话新增打开会话的路径就会漏掉。
  useEffect(() => { setTraceTurn(null) }, [activeSession?.id])
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
    setSidebarOpen(false); setCitation(null); setTraceTurn(null); setTurnResolution(null); setContextUsage(null)
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
      forgetSeen(session.id); setSeen(readSeen())
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
  // 「正在检索知识库」干等一分钟，会当成检索本身慢。配了但还没加载好的才算。
  const modelNote = (['embedding', 'reranker'] as const)
    .filter(key => {
      const slot = healthRag?.[key] as { model?: string; loaded?: boolean; error?: string | null } | undefined
      return !!slot?.model && slot.loaded === false && !slot.error
    })
    .map(key => (key === 'embedding' ? t('model.embedding') : t('model.reranker')))
    .join(t('common.list_sep'))
  // 折叠态只剩 16px 的标识槽，而槽是 aria-hidden——文字一收，这些按钮就全没名字了；
  // 鼠标用户也只看到几条 2.5px 色条。展开态不加，可见文字本身就是名字。
  // 返回类型要显式写：JSX 展开不做多余属性检查，带连字符的属性名 tsc 也一律不查，
  // 不标注的话 title 拼错会静默通过。
  const slotOnly = (name: string): { 'aria-label'?: string; title?: string } =>
    sidebarCollapsed ? { 'aria-label': name, title: name } : {}
  return <LangContext.Provider value={i18n}><DevModeContext.Provider value={dev}><div className={`app ${sidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
    {sidebarOpen && <button className="sidebar-backdrop" aria-label={t('a11y.close_nav')} onClick={() => setSidebarOpen(false)} />}
    <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`} aria-label={t('a11y.sidebar')}>
      <Brand />
      <div className="side-label" id="workspace-group">{t('sidebar.workspaces')}</div>
      {/* role=group 把这几行标成一组、名字取自可见的小标题；「当前是哪个」由 aria-current 说 */}
      <div className="course-switcher" role="group" aria-labelledby="workspace-group">
        {/* 通用模式和课程互斥单选、写的是同一份 workspace 状态，所以并排放在这一组里。
            它不是一门课，这件事靠罗盘图标与副标题表达。
            aria-current 让选中态不只靠颜色和投影，读屏器也拿得到。 */}
        <button className={`course-choice general-choice ${workspace.scope === 'general' ? 'selected' : ''}`}
          aria-current={workspace.scope === 'general' ? 'true' : undefined} {...slotOnly(t('workspace.general'))}
          onClick={() => switchWorkspace({ scope: 'general' })}>
          <span className="slot" aria-hidden><NavIcon name="compass" /></span>
          <span className="cx"><b>{t('workspace.general')}</b><small>{t('workspace.general_hint')}</small></span>
        </button>
        {courses.map(item => <button className={`course-choice ${item.id === workspace.courseId ? 'selected' : ''}`} key={item.id}
          aria-current={item.id === workspace.courseId ? 'true' : undefined} {...slotOnly(item.name)}
          onClick={() => switchWorkspace({ scope: 'course', courseId: item.id })}>
          {/* 竖色条而不是实心圆：7px 实心圆是整个界面里唯一的实心饱和色块，
              而分区靠细边框不靠填充；竖条和 tab 那条 2px 主色下划线是同一个手势。
              Wiki 标记去掉了——它对切课程没有帮助，知识库页顶部有完整状态。 */}
          <span className="slot" aria-hidden><i className="course-bar" style={{ backgroundColor: item.color }} /></span>
          <span className="lb">{item.name}</span>
        </button>)}
        <button className="course-choice add-course" onClick={createCourse} disabled={creating} {...slotOnly(t('course.new'))}>
          <span className="slot" aria-hidden>+</span><span className="lb">{t('course.new')}</span>
        </button>
      </div>
      <div className="side-divider" />

      <nav className="main-nav" aria-label={t('a11y.main_nav')}>
        {/* 可见的「页面」小标题删掉了，当前项靠 aria-current 而不是那个标题被读出来 */}
        {nav.map(item => <button className={view === item.id ? 'active' : ''} key={item.id}
          aria-current={view === item.id ? 'page' : undefined} {...slotOnly(viewName(item.id))}
          onClick={() => { setView(item.id); setSidebarOpen(false) }}>
          <span className="slot" aria-hidden><NavIcon name={item.icon} /></span><b>{viewName(item.id)}</b>
        </button>)}
      </nav>
      <div className="sessions-head" id="session-group"><span>{t('sidebar.sessions')}</span></div>
      <div className="session-list" role="group" aria-labelledby="session-group">
        {sessions.length ? sessions.map(session => <SessionRow key={session.id} session={session}
          active={session.id === activeSession?.id}
          unread={session.id !== activeSession?.id && (!seen[session.id] || session.updated_at > seen[session.id])}
          onOpen={() => { markSeen(session.id); setSeen(readSeen()); setActiveSession(session); setView('chat'); setSidebarOpen(false) }}
          onRename={async title => { await renameSession(title, session.id); markSeen(session.id); setSeen(readSeen()) }}
          onDelete={async () => { await deleteSession(session) }} />) : <p className="mini-empty">{t('session.empty')}</p>}
      </div>
      <button className="new-session" onClick={newSession} disabled={creating}>{t('session.new', { scope: workspace.scope === 'general' ? t('session.scope_general') : t('session.scope_course') })}</button>
      {/* 折叠态把标签 span 收起，只剩那个字形——不给名字的话按钮就叫「问号」「齿轮」 */}
      <div className="sidebar-foot">
        <button onClick={() => { setView('help'); setSidebarOpen(false) }} {...slotOnly(t('nav.help'))}>? <span>{t('nav.help')}</span></button>
        <button onClick={() => { clearCurrentUser(); window.location.reload() }} {...slotOnly(t('user.switch', { name: username }))}>⇄ <span>{t('user.switch', { name: username })}</span></button>
        <button onClick={() => { setView('settings'); setSidebarOpen(false) }} {...slotOnly(t('nav.settings'))}>⚙ <span>{t('nav.settings')}</span></button>
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
      {view === 'chat' && <ChatView session={activeSession} messages={messages} workspaceName={workspaceName} scope={workspace.scope} modelNote={modelNote} turnResolution={turnResolution} contextUsage={contextUsage} draftSeed={draftSeed} onSeedUsed={() => setDraftSeed('')} onCitation={next => { setTraceTurn(null); setCitation(next) }} onUpload={async file => {
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
      {view === 'library' && course && <LibraryView course={course} onCourseChange={updated => setCourses(current => current.map(item => item.id === updated.id ? updated : item))} onError={setNotice} onCitation={setCitation} />}
      {view === 'plan' && course && <PlanView course={course} onError={setNotice} />}
      {view === 'archive' && course && <ArchiveView course={course} onError={setNotice} />}
      {view === 'settings' && <SettingsView courses={courses} onError={setNotice} onCourseDeleted={courseDeleted} />}
      {view === 'help' && <HelpView courses={courses} health={health} onError={setNotice} onTry={text => { setView('chat'); setDraftSeed(text) }} />}
      <footer className="statusbar">
        <span className={apiOnline ? 'ok' : 'bad'}>● {apiOnline ? t('status.connected') : t('status.offline')}</span>
        {/* 掉线时这些都是缓存的旧值，留着会让人以为服务还在 */}
        {apiOnline !== false && healthLlm && <ModelPicker llm={healthLlm} />}
        <LangPicker />
        <span className="right">CoursePilot v2.0</span>
      </footer>
    </main>
    {citation && <CitationDrawer citation={citation} onClose={() => setCitation(null)} onOpen={setCitation} />}
    {devMode && traceTurn && activeSession && <TraceDrawer sessionId={activeSession.id} turnId={traceTurn} onFocus={setTraceTurn} onClose={() => setTraceTurn(null)} />}
  </div></DevModeContext.Provider></LangContext.Provider>
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

// 三处渲染共用一份插件表，加插件时不会漏改其中一处。
// cjk-friendly 治的是 `**术语（英文）**中文` 这种写法——CommonMark 的 flanking 规则会把 ** 原样吐出来。
const REMARK_PLUGINS = [remarkGfm, remarkMath, remarkCjkFriendly]
const REHYPE_PLUGINS = [rehypeKatex]

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

/** 未读：哪些会话在你不在场的时候有了新内容。
 *
 *  纯前端，不动接口：后端每插一条消息都会顺手更新 sessions.updated_at，
 *  所以它就是「最后一条消息的时间」，和本地记的「最后看过的时间」一比就是未读。
 *  已知误报一处：改名也会更新 updated_at，所以改名成功后要顺手记一次已读。 */
const SEEN_KEY = 'cp-seen-sessions'
function readSeen(): Record<string, string> {
  try { return { ...seenFallback, ...JSON.parse(localStorage.getItem(SEEN_KEY) ?? '{}') } } catch { return { ...seenFallback } }
}
// localStorage 写不进时（隐私模式、配额满）退到内存：不兜的话 seen 永远是空的，
// 每个非活动会话都会永久显示未读——比不显示未读更糟。
const seenFallback: Record<string, string> = {}
/** 会话真被删除时才清它的已读记录，不按「当前工作区看到的列表」猜哪些已经不在了——
 *  会话列表是按工作区过滤的，那样会把切走的课程的已读记录当成「已删除」一并清掉，
 *  切回来又变成未读。 */
function forgetSeen(sessionId: string) {
  const seen = readSeen()
  if (!(sessionId in seen)) return
  delete seen[sessionId]
  delete seenFallback[sessionId]
  try { localStorage.setItem(SEEN_KEY, JSON.stringify(seen)) } catch { /* 内存那份上面已经删了 */ }
}

function markSeen(sessionId: string) {
  const seen = readSeen()
  seen[sessionId] = new Date().toISOString()
  seenFallback[sessionId] = seen[sessionId]
  try { localStorage.setItem(SEEN_KEY, JSON.stringify(seen)) } catch { /* 用内存那份兜住 */ }
}

function SessionRow({ session, active, unread, onOpen, onRename, onDelete }: {
  session: SessionSummary; active: boolean; unread: boolean
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

  // 未读只在通用模式下需要课程色（那时会话可能跨课程）：颜色管归属、形状管状态。
  // 课程工作区里所有会话必然同色，一列同色的点信息量是零。
  const showCourseColor = session.scope_mode === 'general'
  return <div className={`session-row ${active ? 'active' : ''} ${unread ? 'unread' : ''}`}>
    <button className="session" onClick={onOpen}
      aria-label={`${session.title || t('session.untitled')}${unread ? ` · ${t('session.unread')}` : ''} · ${session.scope_mode === 'general' ? t('session.general') : t('session.course')}`}>
      <span className="slot" aria-hidden>
        {unread
          ? <i className="st-dot" style={showCourseColor ? { background: session.course_color ?? undefined } : undefined} />
          : showCourseColor ? <i className="course-bar" style={{ backgroundColor: session.course_color ?? '#D4D4D8' }} /> : null}
      </span>
      <span className="session-text"><b>{session.title || t('session.untitled')}</b><small>{timeLabel(session.updated_at)}</small></span>
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
 *  进行中由「正在检索知识库」那句兜着，未命中和停止占位都不上屏。
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
      <div className="message-content"><ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS} components={markdownComponents}>{open.content}</ReactMarkdown></div>
    </div>}
  </article>
}

/** 可折叠树的一个节点。概念目录与知识页共用，各自把自己的行映射成这个形状。
 *  group 标出「这一行是分组而不是内容」，只影响行的视觉。 */
interface TreeItem { id: string; parentId: string; label: string; meta: string; group?: boolean; onOpen?: () => void }

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
      <div className={item.group ? 'concept-row tree-group' : 'concept-row'} key={item.id} style={{ paddingLeft: `${depth * 18}px` }}>
        {kids.length > 0
          ? <button type="button" className={shut ? 'concept-toggle' : 'concept-toggle open'} aria-expanded={!shut} aria-label={item.label} onClick={() => toggle(item.id)}>›</button>
          : <span className="concept-bullet" aria-hidden />}
        {item.onOpen ? <button type="button" className="tree-open" onClick={item.onOpen}>{item.label}</button> : <b>{item.label}</b>}
        {/* 分组行不拼子项数：它的 meta 已经是「共 N 页」，两个含义不同的数字并排容易读混 */}
        <small>{item.meta}{!item.group && kids.length > 0 && ` · ${t('library.concepts_children', { n: kids.length })}`}</small>
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

/** 正文里的出处标注怎么对上出处。三条路依次试：先按（文档、页）严格对；对不上时按
 *  归一化的文档名再对一次（小写、去扩展名、并空格），救回扩展名抄丢这类走样；标注本身
 *  没写文档名时才按页对。按页那一路要自己判歧义——同一页有多条出处（这一页引了多份教材）
 *  时一条都不给，点开哪一条都可能不是这句话的依据。
 *
 *  文档名写了却对不上任何出处时不退到按页：那种标注指的是别的文档，拿本页同页码的出处
 *  顶上去等于把另一份教材当成依据摆给用户看。没有页码的出处按文档名单独收，对应文档级标注。 */
function anchorLookup(anchors: CitationSource[]): (document: string, page: number | null) => CitationSource | undefined {
  const key = (document: string, page: number | null) => `${document}|${page ?? ''}`
  // 真实页面里文档级标注写成 [p.笔记.docx] 的居多（模型把页码前缀也抄了进来），
  // 所以归一化时把开头的 p. 一并去掉。
  const loose = (document: string) => document.toLowerCase().replace(/\s+/g, ' ').trim().replace(/^p\./, '').replace(/\.[a-z0-9]+$/, '')
  // 歧义按归属教材判：文件名可以重名，同名教材的同一页是两条不同的出处，谁都不能接。
  // 一旦判成歧义（null）就保持住，后来的条目不能把它翻回去。
  const owner = (item: CitationSource) => item.material_id || item.document
  const put = (map: Map<string, CitationSource | null>, mapKey: string, item: CitationSource) => {
    if (!map.has(mapKey)) map.set(mapKey, item)
    else if (map.get(mapKey) !== null && owner(map.get(mapKey)!) !== owner(item)) map.set(mapKey, null)
  }
  const byDocument = new Map<string, CitationSource | null>()
  const byLoose = new Map<string, CitationSource | null>()
  const byPage = new Map<number, CitationSource | null>()
  for (const item of anchors) {
    const page = typeof item.page === 'number' ? item.page : null
    put(byDocument, key(item.document, page), item)
    put(byLoose, key(loose(item.document), page), item)
    if (page !== null) byPage.set(page, byPage.has(page) ? null : item)
  }
  return (document, page) => {
    if (document) return byDocument.get(key(document, page)) ?? byLoose.get(key(loose(document), page)) ?? undefined
    return page === null ? undefined : byPage.get(page) ?? undefined
  }
}

// 三种形态都要认：[文档 p.12] 占多数（模型抄的是原文段落自带的标签）、[p.12]、[笔记.docx]。
// 页码位可以是区间（[p.12-14]、[文档 pp.12-14]），点开的是区间第一页。
// 裸形态在引了多份教材的页面上一样会出现，那种歧义由 anchorLookup 的按页那一路判掉。
// 后端体检的 wiki.py:_CITE_MARK 与这里同一口径，改一处要改两处。
const CITE_MARK = /(\[(?:[^\]\n]+ )?pp?\.\d+(?:-\d+)?\]|\[[^\]\n]+\.(?:pdf|docx?|pptx?|txt|md)\])/i
const CITE_PAGE = /^\[(?:(.+) )?pp?\.(\d+)(?:-\d+)?\]$/i
const CITE_DOCUMENT = /^\[([^\]\n]+\.(?:pdf|docx?|pptx?|txt|md))\]$/i
/** 知识页正文里的出处标注：对得上出处的换成可点开原文的小按钮，其余保持原样。
 *  只处理直接文本，代码、公式、强调里的写法不动。 */
function citeMarks(children: ReactNode, anchorAt: (document: string, page: number | null) => CitationSource | undefined, onOpen: (anchor: CitationSource) => void): ReactNode {
  return Children.map(children, child => {
    if (typeof child !== 'string' || !CITE_MARK.test(child)) return child
    return child.split(CITE_MARK).map((part, index) => {
      const paged = CITE_PAGE.exec(part)
      const whole = paged ? null : CITE_DOCUMENT.exec(part)
      const named = paged ? paged[1] ?? '' : whole?.[1]
      if (named === undefined) return part
      const page = paged ? Number(paged[2]) : null
      const anchor = anchorAt(named, page)
      if (!anchor) return part
      const label = page === null ? t('citation.open_document', { document: anchor.document })
        : t('citation.open_source', { document: anchor.document, n: page })
      return <button type="button" className="wiki-cite" key={index} title={label} aria-label={label}
        onClick={() => onOpen(anchor)}>{part}</button>
    })
  })
}

/** 一条体检发现的说明。code 决定文案，其余字段是插值参数；服务端只列前几个页码或文件名，
 *  真正的条数由 n 说明，列表短于 n 时补一个省略号。 */
function lintText(issue: WikiIssue): string {
  const listed: (string | number)[] = issue.pages ?? issue.documents ?? []
  const total = issue.n ?? 0
  return tOr(`library.lint_${issue.code}`, issue.code, {
    n: total, parent: issue.parent ?? '',
    items: listed.join(t('common.list_sep')) + (listed.length < total ? '…' : ''),
  })
}

/** 知识页体检的结论。没发现问题也说一句：不然「没跑过」和「没问题」在界面上是同一个样子。
 *  取不到结果时整块不显示——报告本身是接口，宁可缺席也不能给出一个假的「通过」。 */
function WikiLintNote({ issues }: { issues: WikiIssue[] | null }) {
  const [open, setOpen] = useState(false)
  if (issues === null) return null
  const errors = issues.filter(issue => issue.level === 'error').length
  const summary = [errors > 0 ? t('library.wiki_lint_errors', { n: errors }) : '',
    issues.length > errors ? t('library.wiki_lint_warnings', { n: issues.length - errors }) : ''].filter(Boolean)
  return <div className="wiki-lint">
    <p>{issues.length === 0 ? t('library.wiki_lint_ok')
      : t('library.wiki_lint_found', { summary: summary.join(t('common.list_sep')) })}
      {issues.length > 0 && <button className="text-button" aria-expanded={open} onClick={() => setOpen(!open)}>
        {t(open ? 'library.wiki_lint_hide' : 'library.wiki_lint_show')}</button>}</p>
    {open && <ul>{issues.map((issue, index) => <li className={issue.level} key={`${issue.concept_id}:${issue.code}:${index}`}>
      <b title={issue.concept_name}>{issue.concept_name}</b><span>{lintText(issue)}</span></li>)}</ul>}
  </div>
}

/** 「其他来源也讲了这个」：别的教材里在讲同一件事的页，点一下就翻过去。
 *  边一律跨教材，所以这句话对每一条都成立。服务端读时现算，没有边时整行不显示。 */
function WikiPairRow({ edges, conceptId, onOpen }: { edges: WikiEdge[]; conceptId: string; onOpen: (conceptId: string) => void }) {
  // 服务端已按分数从高到低给出，这里照原序摆。
  const related = useMemo(() => edges
    .filter(edge => edge.a === conceptId || edge.b === conceptId)
    .map(edge => edge.a === conceptId
      ? { id: edge.b, name: edge.b_name, document: edge.b_document }
      : { id: edge.a, name: edge.a_name, document: edge.a_document }), [edges, conceptId])
  if (related.length === 0) return null
  return <div className="wiki-pairs" role="group" aria-labelledby="wiki-pairs-label">
    <span id="wiki-pairs-label">{t('library.wiki_pairs_label')}</span>
    {related.map(item => <button type="button" className="pair-chip" key={item.id} onClick={() => onOpen(item.id)}>
      {item.name}{item.document && <i>{item.document}</i>}
    </button>)}
  </div>
}

/** 保存手写区时的错误。构建中与超限有专门的文案，其余照后端消息显示。 */
function handwrittenError(error: unknown): string {
  const status = error instanceof ApiError ? error.status : undefined
  if (status === 409) return t('library.wiki_hand_busy')
  if (status === 413) return t('library.wiki_hand_too_large')
  return errorText(error)
}

/** 知识页的手写区：分隔线以下归用户，重新生成不覆盖。这里是它唯一的编辑入口。
 *  没写过内容时只留一个低调的入口，不给一段空内容占位。 */
function WikiHandwritten({ courseId, conceptId, text, onSaved }: {
  courseId: string; conceptId: string; text: string; onSaved: (next: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(text)
  const [saving, setSaving] = useState(false)
  const [failure, setFailure] = useState('')
  // 换页与保存成功都要收起编辑器，否则上一页的草稿会留在下一页的输入框里。
  useEffect(() => { setEditing(false); setDraft(text); setFailure('') }, [courseId, conceptId, text])
  function start() { setDraft(text); setFailure(''); setEditing(true) }
  async function save() {
    setSaving(true)
    try {
      onSaved((await api.saveWikiHandwritten(courseId, conceptId, draft)).handwritten)
      setEditing(false); setFailure('')
    } catch (error) { setFailure(handwrittenError(error)) } finally { setSaving(false) }
  }
  if (!editing && !text) return <div className="wiki-hand empty">
    <button type="button" className="text-button" onClick={start}>{t('library.wiki_hand_add')}</button>
    {failure && <p className="danger-text" role="alert">{failure}</p>}
  </div>
  return <section className="wiki-hand">
    <div className="wiki-hand-head"><h3>{t('library.wiki_hand_title')}</h3>
      {!editing && <button type="button" className="text-button" onClick={start}>{t('common.edit')}</button>}</div>
    {editing
      ? <>
          <textarea className="wiki-hand-editor" value={draft} onChange={event => setDraft(event.target.value)}
            placeholder={t('library.wiki_hand_placeholder')} spellCheck={false}
            aria-label={t('a11y.wiki_handwritten')} />
          <div className="wiki-hand-actions">
            <button className="ghost-button" disabled={saving} onClick={() => void save()}>
              {saving ? t('library.wiki_hand_saving') : t('common.save')}</button>
            <button className="ghost-button" disabled={saving} onClick={() => { setEditing(false); setDraft(text); setFailure('') }}>
              {t('common.cancel')}</button>
            <span>{t('library.wiki_hand_hint')}</span>
          </div>
        </>
      : <div className="message-content"><ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS}
          components={markdownComponents}>{text}</ReactMarkdown></div>}
    {failure && <p className="danger-text" role="alert">{failure}</p>}
  </section>
}

/** 教材分组行的 id 前缀。知识页 id 是 index / section_ / concept_ 这几种，加前缀不会撞上。 */
const WIKI_GROUP = 'material:'

/** 教材已删、只剩 id 时显示名里带哪一段。id 形如 material_<32hex>，
 *  前缀每份都一样，剥掉才看得出这是两本不同的书。 */
function shortMaterialId(id: string): string {
  return (id.startsWith('material_') ? id.slice(9) : id).slice(0, 8)
}

/** 知识页树的行。多教材课程在树根加一层教材分组，一本书一个可折叠节点；
 *  单教材课程不加这层。没记归属的页（课程总览、旧格式页）留在根层，不并进任何一组。 */
function wikiTreeItems(pages: WikiPageSummary[], onOpen: (page: WikiPageSummary) => void): TreeItem[] {
  const row = (page: WikiPageSummary, parentId: string): TreeItem => ({
    id: page.concept_id, parentId, label: page.concept_name,
    meta: t('library.updated_at', { time: page.updated_at.slice(0, 16).replace('T', ' ') }),
    onOpen: () => onOpen(page),
  })
  const flat = () => pages.map(page => row(page, page.parent_id ?? ''))
  const owners = new Set(pages.map(page => page.material_id || '').filter(Boolean))
  if (owners.size < 2) return flat()
  // 手改 frontmatter 能让页 id 撞上分组行的 id，两者合成一个指向自己的节点，
  // treeRows 顺着递归下去爆栈、整页白屏。撞上就整棵树不分组。
  const taken = new Set(pages.map(page => page.concept_id))
  if ([...owners].some(owner => taken.has(WIKI_GROUP + owner))) return flat()

  const byId = new Map(pages.map(page => [page.concept_id, page] as const))
  const groupOf = (page: WikiPageSummary) => page.material_id ? WIKI_GROUP + page.material_id : ''
  const sameBook = (page: WikiPageSummary) => {
    const parent = page.parent_id ? byId.get(page.parent_id) : undefined
    return parent && (parent.material_id || '') === (page.material_id || '') ? parent : undefined
  }
  // 挂父页的前提：整条链都在同一本书里、而且走得到顶。跨教材的父页与手改出的环
  // 一律挂回自己那本书——分组里只出现这本书的页，「共 N 页」才和画出来的行数对得上。
  const parentOf = (page: WikiPageSummary): string => {
    const parent = sameBook(page)
    if (!parent) return groupOf(page)
    const seen = new Set([page.concept_id])
    for (let up: WikiPageSummary | undefined = parent; up; up = sameBook(up)) {
      if (seen.has(up.concept_id)) return groupOf(page)
      seen.add(up.concept_id)
    }
    return parent.concept_id
  }
  const counts = new Map<string, number>()
  const names = new Map<string, string>()
  for (const page of pages) {
    const owner = page.material_id || ''
    if (!owner) continue
    counts.set(owner, (counts.get(owner) ?? 0) + 1)
    if (page.document) names.set(owner, page.document)
  }
  // 同名教材，以及两份已删教材 id 前几位相同时，两行会显示成同一个名字。
  // 按分组出现的先后加序号，先出现的那本保留原名。
  const used = new Map<string, number>()
  const distinct = (label: string) => {
    const nth = (used.get(label) ?? 0) + 1
    used.set(label, nth)
    return nth > 1 ? t('library.wiki_group_dup', { name: label, n: nth }) : label
  }
  const rows = pages.map(page => row(page, parentOf(page)))
  const groups: TreeItem[] = [...counts].map(([owner, n]) => ({
    id: WIKI_GROUP + owner, parentId: '', group: true,
    label: distinct(names.get(owner) ?? t('library.wiki_group_gone', { id: shortMaterialId(owner) })),
    meta: t('library.wiki_group_pages', { n }),
  }))
  // 根层的先后：没记归属的页在前（课程总览排最上），教材分组跟在后面。
  return [...rows.filter(item => !item.parentId), ...groups, ...rows.filter(item => item.parentId)]
}

/** 已生成的 Wiki 页，按教材目录嵌成一棵可折叠的树。没有层级的教材照旧平铺。
 *  正文里的出处标注与 frontmatter 的 source_refs 对得上，点开的是那一页教材原文。 */
function WikiPagesPanel({ course, refreshKey, onError, onCitation }: { course: Course; refreshKey: number; onError: (message: string) => void; onCitation: (citation: Citation) => void }) {
  const [pages, setPages] = useState<WikiPageSummary[] | null>(null)
  const [issues, setIssues] = useState<WikiIssue[] | null>(null)
  const [edges, setEdges] = useState<WikiEdge[]>([])
  // body 是系统生成的那半，handwritten 是用户自己写的，两段分开渲染，分隔标记不上屏。
  const [open, setOpen] = useState<{ course: string; id: string; title: string; body: string; handwritten: string; anchors: CitationSource[] } | null>(null)
  // 连点两页时慢的那个响应会后到。只认最后一次点击的编号，别让它盖掉现在显示的页。
  const latest = useRef(0)
  // 正文的滚动容器换页时是复用的同一个节点，不主动滚回顶部，新页会停在上一页的位置。
  const viewer = useRef<HTMLDivElement>(null)
  // 切课时同理：上一门课的体检结果可能后到，落到新课的面板上。
  const loaded = useRef(0)
  const { collapsed, toggle, toggleAll } = useCollapse(`${course.id}:${refreshKey}`)
  const { lang } = useI18n()
  useEffect(() => {
    const ticket = ++loaded.current
    setPages(null); setOpen(null); setIssues(null); setEdges([])
    api.wikiPages(course.id).then(payload => { if (loaded.current === ticket) setPages(payload.pages) })
      .catch(error => { if (loaded.current === ticket) { setPages([]); onError(errorText(error)) } })
    // 体检取不到就不显示那一行，页面本身照读。
    api.wikiLint(course.id).then(payload => { if (loaded.current === ticket) setIssues(payload.issues) }).catch(() => {})
    // 配对同理：取不到就当这门课没有边，读页不受影响。
    api.wikiGraph(course.id).then(payload => { if (loaded.current === ticket) setEdges(payload.edges) }).catch(() => {})
  }, [course.id, refreshKey])
  async function read(page: WikiPageSummary) {
    const ticket = ++latest.current
    try {
      const payload = await api.wikiPage(course.id, page.concept_id)
      if (latest.current !== ticket) return
      setOpen({ course: course.id, id: page.concept_id, title: page.concept_name,
        // 分段字段缺席只可能是服务端还没跟上，那时整页照旧剥掉 frontmatter 渲染。
        body: payload.body ?? stripFrontmatter(payload.content), handwritten: payload.handwritten ?? '', anchors: [] })
    } catch (error) { if (latest.current === ticket) onError(errorText(error)); return }
    // 出处单独取：拿不到就让出处标注留成纯文本，正文照样读得了。
    try {
      const { anchors } = await api.wikiPageSources(course.id, page.concept_id)
      if (latest.current !== ticket) return
      setOpen(current => current && current.id === page.concept_id && current.course === course.id ? { ...current, anchors } : current)
    } catch { /* 出处取不到不影响正文 */ }
  }
  useEffect(() => { viewer.current?.scrollTo({ top: 0 }) }, [open?.id])
  // 出处表只在换页或取回出处时重算一次。memo 里调了 t()，lang 要进依赖。
  const components = useMemo(() => {
    const anchorAt = anchorLookup(open?.anchors ?? [])
    const mark = (children: ReactNode) => citeMarks(children, anchorAt, anchor => onCitation(asMaterial(anchor)))
    // node 之外的 props 全部透传：className 与 style 由 GFM 给出（任务列表、表格对齐），
    // 挑着透传的话，remark 换个方式表达对齐时这里会静默丢掉它。
    return {
      ...markdownComponents,
      p: ({ children, node, ...rest }: ComponentProps<'p'> & { node?: unknown }) => <p {...rest}>{mark(children)}</p>,
      li: ({ children, node, ...rest }: ComponentProps<'li'> & { node?: unknown }) => <li {...rest}>{mark(children)}</li>,
      td: ({ children, node, ...rest }: ComponentProps<'td'> & { node?: unknown }) => <td {...rest}>{mark(children)}</td>,
    }
  }, [open?.anchors, onCitation, lang])
  // lang 要进依赖：memo 里调了 t()，换语言时数据没变，缓存的旧译文会和现算的部分混排。
  const items = useMemo(() => wikiTreeItems(pages ?? [], page => void read(page)), [pages, lang])
  const children = useMemo(() => groupByParent(items), [items])
  const branches = useMemo(() => items.filter(item => (children.get(item.id) ?? []).length > 0), [items, children])
  // 层级说的是页与页之间，教材分组这一层不算：平铺的多教材课照旧要给出那句说明。
  const nested = useMemo(() => branches.some(item => !item.group), [branches])
  if (pages !== null && pages.length === 0) return null
  return <article className="card">
    <div className="card-heading">
      <div><h2>{pages ? t('library.wiki_pages_title_n', { n: pages.length }) : t('library.wiki_pages_title')}</h2>
        <p>{t('library.wiki_pages_hint')}</p></div>
      {branches.length > 0 && <button className="text-button" onClick={() => toggleAll(branches)}>
        {collapsed.size ? t('library.concepts_expand_all') : t('library.concepts_collapse_all')}</button>}
    </div>
    <WikiLintNote issues={issues} />
    {pages === null ? <p className="mini-empty">{t('common.loading')}</p>
      : <div className="concept-tree">
          {!nested && <p className="wiki-note">{t('library.wiki_pages_flat_note')}</p>}
          {treeRows(children, collapsed, toggle)}
        </div>}
    {open && <div className="note-viewer">
      <div className="note-viewer-head"><b>{open.title}</b><button onClick={() => setOpen(null)} aria-label={t('a11y.close_wiki')}>×</button></div>
      <div className="message-content" ref={viewer}><ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS} components={components}>{open.body}</ReactMarkdown></div>
      <WikiHandwritten courseId={course.id} conceptId={open.id} text={open.handwritten}
        onSaved={handwritten => setOpen(current => current && current.id === open.id ? { ...current, handwritten } : current)} />
      <WikiPairRow edges={edges} conceptId={open.id} onOpen={conceptId => {
        const page = (pages ?? []).find(item => item.concept_id === conceptId)
        if (page) void read(page)
      }} />
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

/** 上下文占比环。r=7、周长 2πr≈43.98，用 dashoffset 表示已用比例，起点转到 12 点。 */
function UsageRing({ percent, warn, size = 14 }: { percent: number; warn?: boolean; size?: number }) {
  const circumference = 2 * Math.PI * 7
  return <svg width={size} height={size} viewBox="0 0 20 20" aria-hidden focusable="false">
    <circle className="ring-track" cx="10" cy="10" r="7" fill="none" strokeWidth="3" />
    <circle className={warn ? 'ring-fill warn' : 'ring-fill'} cx="10" cy="10" r="7" fill="none" strokeWidth="3"
      strokeDasharray={circumference.toFixed(2)}
      strokeDashoffset={(circumference * (1 - Math.min(1, percent / 100))).toFixed(2)}
      transform="rotate(-90 10 10)" />
  </svg>
}

// 占比小于这个数的分区折进「其余 n 项」：原来 11 行里有三行不到 0.5%，
// 各占一整行，把真正的大头夹在中间。
const CONTEXT_MINOR_SHARE = 2

function ContextMeter({ usage }: { usage: ContextUsage }) {
  const [open, setOpen] = useState(false)
  const [showMinor, setShowMinor] = useState(false)
  const k = (tokens: number) => tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}K` : String(tokens)
  const percent = Math.min(100, Math.round((usage.total_tokens / usage.limit_tokens) * 100))
  const gated = usage.gate_tools_cleared > 0 || usage.gate_history_dropped > 0 || usage.gate_evidence_clipped
  const notice = usage.dropped_history > 0 || usage.clipped_history > 0 || usage.clipped_segments.length > 0 || gated
  // 要回答的问题是「哪块吃掉了上下文」，所以按占比降序，而不是按服务端给的顺序
  const sum = usage.segments.reduce((total, segment) => total + segment.tokens, 0) || 1
  const ranked = [...usage.segments]
    .map(segment => ({ ...segment, share: segment.tokens / sum * 100, name: segment.label_key ? tOr(segment.label_key, segment.label) : segment.label }))
    .sort((a, b) => b.tokens - a.tokens)
  const major = ranked.filter(segment => segment.share >= CONTEXT_MINOR_SHARE)
  const minor = ranked.filter(segment => segment.share < CONTEXT_MINOR_SHARE)
  const minorTokens = minor.reduce((total, segment) => total + segment.tokens, 0)
  return <div className="context-chip">
    <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} aria-label={t('a11y.context')} className={notice ? 'warn' : undefined}>
      <UsageRing percent={percent} warn={notice} />
      <b>{percent}%</b>
    </button>
    {open && <div className="context-popover">
      <div className="popover-head">
        <span className="popover-title"><b>{t('context.title')}</b><small>{k(usage.total_tokens)} / {k(usage.limit_tokens)}</small></span>
        <span className="popover-ring"><UsageRing percent={percent} warn={notice} size={40} /><i>{percent}%</i></span>
      </div>
      <div className="usage-stack" role="img" aria-label={t('a11y.context')}>
        {ranked.map((segment, index) => <i key={segment.label} style={{ width: `${segment.share}%`, background: `var(--stack-${Math.min(index, 6)})` }} />)}
      </div>
      {major.map((segment, index) => <div className="usage-row" key={segment.label}>
        <i style={{ background: `var(--stack-${Math.min(index, 6)})` }} />
        <span>{segment.name}</span>
        <b>{k(segment.tokens)} · {Math.round(segment.share)}%</b>
      </div>)}
      {minor.length > 0 && (showMinor
        ? minor.map((segment, index) => <div className="usage-row" key={segment.label}>
            <i style={{ background: `var(--stack-${Math.min(major.length + index, 6)})` }} />
            <span>{segment.name}</span>
            <b>{k(segment.tokens)} · &lt;{CONTEXT_MINOR_SHARE}%</b>
          </div>)
        : <button type="button" className="usage-more" onClick={() => setShowMinor(true)}>
            {t('context.minor_more', { n: minor.length, tokens: k(minorTokens) })}
          </button>)}
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

/** 周网格。取代原来的 mermaid 甘特图，顺带修掉它三个毛病：
 *  任务名 slice(0,16) 硬截断不加省略号（看着像渲染坏了）、每条都是 1d 让日期轴刻度重复、
 *  状态色只在「今天/过期/完成」三档有颜色——新排的计划全是未来日期，整张图一片同色。
 *
 *  选型上甘特也不合适：它表达跨度与依赖，而这里每条都是「某一天读某几页」，两者都没有。 */
function PlanWeeks({ items }: { items: Plan['items'] }) {
  const today = new Date().toLocaleDateString('sv')   // sv locale 就是 YYYY-MM-DD
  if (!items.length) return null

  // 按 setDate 走而不是加毫秒：在午夜切换夏令时的时区（Santiago、Cairo、Beirut）
  // 加 86400000 会丢一天或重复一天，那天的条目就从网格里消失了。
  const shift = (date: string, days: number) => {
    const at = new Date(`${date}T00:00:00`)
    at.setDate(at.getDate() + days)
    return at.toLocaleDateString('sv')
  }
  const mondayOf = (date: string) => {
    const weekday = (new Date(`${date}T00:00:00`).getDay() + 6) % 7   // 周一为 0
    return shift(date, -weekday)
  }
  const byDay = new Map<string, Plan['items']>()
  for (const item of items) byDay.set(item.due_date, [...(byDay.get(item.due_date) ?? []), item])

  // 有条目的那几周都列出来，不做翻页——翻页会藏掉信息，而甘特图原来是能一眼看全的
  const weeks = [...new Set([...byDay.keys()].map(mondayOf))].sort()
  const label = (date: string) => date.slice(5).replace('-', '/')

  return <div className="plan-weeks">
    {weeks.map(monday => {
      const days = Array.from({ length: 7 }, (_, offset) => shift(monday, offset))
      const count = days.reduce((total, day) => total + (byDay.get(day)?.length ?? 0), 0)
      return <section className="week" key={monday}>
        <header><b>{t('plan.week_range', { from: label(days[0]), to: label(days[6]) })}</b><span>{t('plan.item_count', { n: count })}</span></header>
        <div className="week-head">{t('plan.weekday_short').split(',').map((name, index) => <span key={index}>{name}</span>)}</div>
        <div className="week-body">
          {days.map(day => {
            const state = day === today ? 'today' : day < today ? 'past' : ''
            return <div className={`day ${state}`} key={day}>
              <time dateTime={day}>{label(day)}{day === today && <em>{t('plan.today')}</em>}</time>
              {(byDay.get(day) ?? []).map(item => {
                const mark = item.status === 'done' ? 'done' : day < today ? 'late' : ''
                return <div className={`task ${mark}`} key={item.id}>
                  <b>{item.title}</b>
                  {item.concept_name && <small>{item.concept_name}</small>}
                </div>
              })}
            </div>
          })}
        </div>
      </section>
    })}
    <div className="week-legend">
      <span><i className="todo" />{t('plan.legend_todo')}</span>
      <span><i className="done" />{t('plan.legend_done')}</span>
      <span><i className="late" />{t('plan.legend_late')}</span>
    </div>
  </div>
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

/** 来源名与出处分成两列：出处右对齐成列，才扫得出「一共引了哪几页」。 */
function citationParts(item: Citation): { who: string; at: string } {
  if (item.kind === 'web') return { who: item.title || item.url || t('citation.web'), at: t('citation.at_web') }
  if (item.kind === 'wiki') return {
    who: t('citation.wiki_chip', { name: item.concept_name || item.concept_id || t('citation.wiki_fallback') }),
    at: t('citation.at_wiki'),
  }
  return {
    who: item.material_name ?? t('citation.material_fallback'),
    at: item.page ? t('citation.at_page', { n: item.page }) : t('citation.at_unknown'),
  }
}

function CitationRow({ item, fallbackNumber, onOpen }: { item: Citation; fallbackNumber: number; onOpen: (citation: Citation) => void }) {
  const { who, at } = citationParts(item)
  const kind = item.kind === 'wiki' ? 'wiki' : item.kind === 'web' ? 'web' : 'mat'
  const body = <>
    <span className="cite-no">{item.number ?? fallbackNumber}</span>
    <span className="cite-who">{who}</span>
    <span className="cite-at">{at}</span>
  </>
  const href = item.kind === 'web' ? safeHref(item.url) : null
  if (href) return <a className={`cite-row ${kind}`} href={href} target="_blank" rel="noopener noreferrer nofollow" title={item.url}>{body}</a>
  return <button type="button" className={`cite-row ${kind}`} onClick={() => onOpen(item)}>{body}</button>
}

/** 依据面板。三类来源用左侧色条 + 出处成列区分，不再靠颜色深浅。 */
function CitationPanel({ items, onOpen }: { items: Citation[]; onOpen: (citation: Citation) => void }) {
  const counts = { mat: 0, wiki: 0, web: 0 }
  for (const item of items) counts[item.kind === 'wiki' ? 'wiki' : item.kind === 'web' ? 'web' : 'mat'] += 1
  const summary = [
    counts.mat ? t('citation.count_material', { n: counts.mat }) : '',
    counts.wiki ? t('citation.count_wiki', { n: counts.wiki }) : '',
    counts.web ? t('citation.count_web', { n: counts.web }) : '',
  ].filter(Boolean).join(' · ')
  return <div className="citations">
    <div className="citations-head"><b>{t('citation.panel_title')}</b><span>{summary}</span></div>
    {items.map((item, index) => <CitationRow key={item.id ?? item.chunk_id ?? item.url ?? index} item={item} fallbackNumber={index + 1} onOpen={onOpen} />)}
  </div>
}

function MessageCard({ message, onCitation, showResolution, onRetry, modelNote, onChoose }: { message: Message; onCitation: (citation: Citation) => void; showResolution: boolean; onRetry?: () => void; modelNote?: string; onChoose?: (text: string) => void }) {
  if (message.role === 'user') return <article className="message user-message"><div>{message.content}</div></article>
  const isInterrupted = message.artifact?.kind === 'interrupted' || message.status === 'interrupted'
  // 课程会话的课程是固定的，逐条标注解析结果只会制造噪音；仅通用会话展示。
  const resolution = !showResolution ? null : message.resolution_status === 'resolved' ? t('message.resolved', { course: message.resolved_course_name ?? message.resolved_course_id ?? t('message.course_fallback') }) : message.resolution_status ? t('message.unresolved') : null
  return <article className="message assistant-message"><AgentLabel turnId={message.turn_id ?? null} />{message.activity && message.activity.length > 0 && <ToolActivityRow activity={message.activity} />}
    {message.status === 'stopped' && <div className="degraded-notice"><span>{t('message.stopped_note')}</span>{onRetry && <button type="button" className="ghost-button" onClick={onRetry}>{t('message.retry')}</button>}</div>}
    {message.degraded && <div className="degraded-notice">{t('message.degraded_note', { note: message.degraded })}</div>}<div className={message.status === 'streaming' ? 'message-content streaming' : 'message-content'}>{message.content ? <ReactMarkdown remarkPlugins={REMARK_PLUGINS} rehypePlugins={REHYPE_PLUGINS} components={markdownComponents}>{message.content}</ReactMarkdown> : <ThinkingHint activity={message.activity} modelNote={modelNote} />}</div>{resolution && <span className={`message-resolution ${message.resolution_status === 'resolved' ? 'resolved' : ''}`}>{resolution}</span>}{isInterrupted && <div className="interrupted"><span>{t('message.interrupted')}</span>{onRetry && <button type="button" className="ghost-button" onClick={onRetry}>{t('message.retry')}</button>}</div>}{message.citations && message.citations.length > 0 && <CitationPanel items={message.citations} onOpen={onCitation} />}{message.artifact && message.artifact.visibility !== 'model_private' && message.artifact.kind !== 'interrupted' && <div className="artifact-card"><b>{t('message.artifact_public')}</b><span>{message.artifact.kind}</span></div>}{message.choices && message.choices.length > 0 && onChoose && <div className="choices">{message.choices.map(option => <button type="button" className="choice" key={option} onClick={() => onChoose(option)}>{option}</button>)}</div>}</article>
}

function LibraryView({ course, onCourseChange, onError, onCitation }: { course: Course; onCourseChange: (course: Course) => void; onError: (message: string) => void; onCitation: (citation: Citation) => void }) {
  const [tab, setTab] = useState<'rag' | 'concepts' | 'wiki' | 'notes'>('rag'); const [materials, setMaterials] = useState<Material[]>([]); const [jobs, setJobs] = useState<Record<string, Job>>({}); const [searchQuery, setSearchQuery] = useState(''); const [results, setResults] = useState<SearchResult[]>([]); const [searched, setSearched] = useState(''); const [loading, setLoading] = useState(false); const fileInput = useRef<HTMLInputElement>(null)
  const [ragBackend, setRagBackend] = useState<string>('')
  const polling = useRef(false)
  const [ocrTarget, setOcrTarget] = useState<string>(''); const [ocrEstimate, setOcrEstimate] = useState<OcrEstimate | null>(null); const [ocrRunning, setOcrRunning] = useState(false)
  const [wikiEstimates, setWikiEstimates] = useState<Record<string, WikiEstimate>>({})
  // 构建收尾的覆盖率报告存在任务记录里；刷新后内存里的 jobs 是空的，按教材回读最近一次。
  const [wikiReports, setWikiReports] = useState<Record<string, Job>>({})
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
  // 每次整表替换：换课程时旧课的报告不能留在屏幕上。轮询那边合并是因为期间可能有新任务，
  // 这边是一次性回读一门课的全部教材，合并只会把上一门课的留下。没有报告的教材不占位。
  useEffect(() => {
    if (tab !== 'wiki' || !course.wiki_enabled) return
    let cancelled = false
    void (async () => {
      const pairs = await Promise.all(indexedMaterials.map(async item => {
        try { const { job } = await api.wikiReport(item.id); return job ? [item.id, job] as const : null } catch { return null }
      }))
      if (!cancelled) setWikiReports(Object.fromEntries(pairs.filter(Boolean) as [string, Job][]))
    })()
    return () => { cancelled = true }
  }, [tab, course.id, course.wiki_enabled, indexedIds, wikiDone])
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
      // 状态小字、报告与按钮文案都读它：本次构建的任务记录更新，没有才退回落库的那份报告，
      // 否则刷新之后建过的教材会显示成没建过。
      const noteJob = wikiJob ?? wikiReports[material.id]
      return <div className="material-row" key={material.id}><div className="file-mark">{fileKind(material)}</div><div className="material-copy"><b>{material.filename ?? material.name ?? t('library.material_untitled')}</b><small>{noteJob ? stageLabel(noteJob.stage ?? noteJob.status, String(noteJob.status)) : t('library.wiki_ready')}</small>{!running && <WikiEstimateNote estimate={wikiEstimates[material.id]} />}{wikiJob && <div className="job-progress"><i style={{ width: `${wikiJob.progress ?? 15}%` }} /></div>}{noteJob?.error && <WikiBuildNote job={noteJob} />}</div><button className="ghost-button" onClick={() => void buildWiki(material.id)} disabled={running}>{noteJob && !running ? t('library.wiki_rebuild') : t('library.wiki_build')}</button></div>
    }) : <div className="empty-inline">{t('library.wiki_needs_material')}</div>}</> : <div className="empty-inline"><b>{t('library.wiki_off_title')}</b><p>{t('library.wiki_off_body')}</p></div>}</article>{course.wiki_enabled && <WikiPagesPanel course={course} refreshKey={wikiDone} onError={onError} onCitation={onCitation} />}</>}</div></section>
}

/** 构建前的账单。页数与调用次数是离线算的，分钟数按每页约 5 秒外推。 */
function WikiEstimateNote({ estimate }: { estimate?: WikiEstimate }) {
  if (!estimate) return <small className="wiki-coverage">{t('library.wiki_estimating')}</small>
  return <small className="wiki-coverage">
    {t('library.wiki_estimate', { pages: estimate.pages, calls: estimate.calls, minutes: estimate.minutes })}
    {estimate.merged > 0 && ` ${t('library.wiki_estimate_merged', { n: estimate.merged })}`}
    {!estimate.has_levels && ` ${t('library.wiki_estimate_flat')}`}
    {estimate.outline === 'concepts' && ` ${t('library.wiki_outline_fallback')}`}
  </small>
}

/** Wiki 构建的收尾提示。成功时后端给的是覆盖率字段串，按语言渲染；失败时原样显示报错。 */
function WikiBuildNote({ job }: { job: Job }) {
  const raw = job.error ?? ''
  if (!raw.startsWith('wiki_coverage ')) return <small className="danger-text">{raw}</small>
  const fields: Record<string, string> = {}
  for (const item of raw.split(' ').slice(1)) {
    const [key, value] = item.split('=')
    fields[key] = value ?? ''
  }
  const count = (key: string) => Number(fields[key]) || 0
  return <small className="wiki-coverage">
    {t('library.wiki_coverage', { concepts: count('concepts'), pages: count('pages') })}
    {count('merged') > 0 && ` ${t('library.wiki_coverage_merged', { merged: count('merged') })}`}
    {' '}{t('library.wiki_coverage_detail', { written: count('written'), skipped: count('skipped') })}
    {fields.outline === 'concepts' && ` ${t('library.wiki_outline_fallback')}`}
    {count('oversized') > 0 && ` ${t('library.wiki_coverage_oversized', { n: count('oversized') })}`}
    {count('issues') > 0 && ` ${t('library.wiki_coverage_issues', { n: count('issues') })}`}
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
    {!loaded ? <p className="mini-empty">{t('plan.loading')}</p> : error ? <RetryCard title={t('plan.error_title')} message={error} onRetry={() => setAttempt(n => n + 1)} /> : plan ? <article className="card"><div className="card-heading"><div><h2>{t('plan.current_title')}</h2><p>{t('plan.meta', { version: plan.version, n: plan.items.length, time: plan.updated_at.slice(0, 16).replace('T', ' ') })}</p></div></div><PlanWeeks items={plan.items} /><PlanDays items={plan.items} /></article> : <article className="card"><h2>{t('plan.empty_title')}</h2><p>{t('plan.empty_body')}</p></article>}
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

function DeveloperCard() {
  const { enabled, setEnabled } = useDevMode()
  return <article className="card dev-card"><h2>{t('settings.dev_title')}</h2>
    <p>{t('settings.dev_hint')}</p>
    <label className="dev-toggle">
      <input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} />
      <span>{t('settings.dev_toggle')}</span>
    </label>
    <small className="help-note">{t('settings.dev_note')}</small>
  </article>
}

/** 接入的 MCP server。工具清单是连接那一刻拉下来的快照，运行期只用它。 */
function McpCard({ onError }: { onError: (message: string) => void }) {
  const [servers, setServers] = useState<McpServer[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [form, setForm] = useState({ label: '', url: '', credential: '' })
  const [confirming, setConfirming] = useState('')
  async function reload() {
    try { setServers((await api.mcpServers()).servers) }
    catch (error) { setServers([]); onError(errorText(error)) }
  }
  useEffect(() => { void reload() }, [])
  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    try { await action(); await reload() } catch (error) { onError(errorText(error)) } finally { setBusy(false) }
  }
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!form.url.trim()) return
    await run(async () => {
      await api.addMcpServer({ label: form.label.trim() || form.url.trim(), url: form.url.trim(), credential: form.credential })
      setForm({ label: '', url: '', credential: '' })
    })
  }
  return <article className="card"><h2>{t('mcp.title')}</h2>
    <p>{t('mcp.hint')}</p>
    {servers === null ? <p className="empty-inline">{t('common.loading')}</p>
      : servers.length === 0 ? <p className="empty-inline">{t('mcp.empty')}</p>
      : servers.map(server => <div className="skill-row" key={server.id}>
        <div className="skill-copy">
          <b>{server.label}<em>{server.origin === 'model' ? t('mcp.origin_model') : t('mcp.origin_user')}</em></b>
          <small className="skill-tools">{server.url}</small>
          <small>{server.tools.length ? t('mcp.tools_count', { n: server.tools.length }) : t('mcp.tools_none')}
            {server.has_credential ? ` · ${t('mcp.has_credential')}` : ''}
            {server.server_info ? ` · ${server.server_info}` : ''}</small>
          {server.status === 'proposed' && <small className="help-note">{t('mcp.pending_note')}</small>}
          {server.dropped_at_snapshot > 0 && <small className="skill-denied">{t('mcp.dropped_snapshot', { total: server.tools_total, kept: server.tools.length, n: server.dropped_at_snapshot })}</small>}
          {server.dropped_at_downlink > 0 && <small className="skill-denied">{t('mcp.dropped_downlink', { n: server.dropped_at_downlink })}</small>}
          {server.last_error_code && <small className="skill-denied" title={server.last_error_detail}>
            {tOr(`mcp.error.${server.last_error_code}`, server.last_error_detail)}</small>}
          {confirming === server.id && <DangerConfirm
            what={t('mcp.delete_what')}
            consequences={[t('mcp.delete.c1'), t('mcp.delete.c2')]}
            onConfirm={() => { setConfirming(''); void run(() => api.deleteMcpServer(server.id)) }}
            onCancel={() => setConfirming('')} />}
        </div>
        <div className="skill-actions">
          <span className={`skill-status ${server.status}`}>{tOr(`mcp.status.${server.status}`, server.status)}</span>
          <button className="ghost-button" disabled={busy} onClick={() => void run(() => api.connectMcpServer(server.id))}>
            {server.status === 'proposed' ? t('mcp.approve') : t('mcp.reconnect')}
          </button>
          {server.tools.length > 0 && server.status !== 'proposed' && <button className="ghost-button" disabled={busy} onClick={() => void run(() => api.setMcpEnabled(server.id, server.status !== 'connected'))}>
            {server.status === 'connected' ? t('mcp.disable') : t('mcp.enable')}
          </button>}
          <button className="ghost-button danger" disabled={busy} onClick={() => setConfirming(server.id)}>{t('common.delete')}</button>
        </div>
      </div>)}
    <form className="mcp-form" onSubmit={submit}>
      <input value={form.label} placeholder={t('mcp.label_placeholder')} aria-label={t('mcp.label')} onChange={event => setForm({ ...form, label: event.target.value })} />
      <input value={form.url} placeholder={t('mcp.url_placeholder')} aria-label={t('mcp.url')} onChange={event => setForm({ ...form, url: event.target.value })} />
      <input value={form.credential} type="password" placeholder={t('mcp.credential_placeholder')} aria-label={t('mcp.credential')} onChange={event => setForm({ ...form, credential: event.target.value })} />
      <button className="ghost-button" type="submit" disabled={busy || !form.url.trim()}>{t('mcp.add')}</button>
    </form>
    <small className="help-note">{t('mcp.credential_note')}</small>
  </article>
}

function SettingsView({ courses, onError, onCourseDeleted }: { courses: Course[]; onError: (message: string) => void; onCourseDeleted: (courseId: string) => void }) {
  const [health, setHealth] = useState<Record<string, unknown> | null>(null)
  const [loading, setLoading] = useState(false)
  async function check() { setLoading(true); try { setHealth(await api.health()) } catch (error) { onError(errorText(error)) } finally { setLoading(false) } }
  const llm = (health?.llm ?? null) as Record<string, unknown> | null
  const rag = (health?.rag ?? null) as Record<string, unknown> | null
  const embedding = (rag?.embedding ?? null) as Record<string, unknown> | null
  return <section className="page"><div className="page-inner"><div className="hero"><div><h1>{t('nav.settings')}</h1><p>{t('settings.hero')}</p></div><button className="ghost-button" onClick={check} disabled={loading}>{t('settings.check')}</button></div><div className="settings-grid"><article className="card"><h2>{t('settings.courses_title')}</h2><p>{t('settings.courses_count', { n: courses.length })}</p>{courses.length ? courses.map(course => <CourseSettingRow key={course.id} course={course} onDelete={onCourseDeleted} onError={onError} />) : <p className="empty-inline">{t('settings.courses_empty')}</p>}</article><MemoryCard courses={courses} onError={onError} /><SkillsCard onError={onError} /><McpCard onError={onError} /><DeveloperCard /><article className="card health-card"><h2>{t('settings.health_title')}</h2>{health ? <><dl>
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
function sourceSpans(sources: CitationSource[]): { key: string; document: string; pages: string; items: CitationSource[] }[] {
  // 按归属教材分组：两本同名书各成一组，不能并成一条页码区间。显示名仍是文件名。
  const grouped = new Map<string, CitationSource[]>()
  for (const item of sources) {
    const key = `${item.material_id ?? ''}|${item.document}`
    grouped.set(key, [...(grouped.get(key) ?? []), item])
  }
  // 同名教材的两组显示名一模一样，按分组出现的先后加序号，先出现的保留原名（与知识页树同一套写法）。
  // 出处顺序由服务端给定，同一份数据每次渲染的序号都一样。
  const used = new Map<string, number>()
  return [...grouped].map(([key, items]) => {
    const numbers = items.map(item => item.page).filter((page): page is number => typeof page === 'number')
    const range = numbers.length === 0 ? '' : numbers[0] === numbers[numbers.length - 1]
      ? String(numbers[0]) : `${numbers[0]}–${numbers[numbers.length - 1]}`
    const name = items[0].document
    const nth = (used.get(name) ?? 0) + 1
    used.set(name, nth)
    return { key, document: nth > 1 ? t('library.wiki_group_dup', { name, n: nth }) : name, pages: range, items }
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
    {sourceSpans(sources).map(span => <div className="citation-span" key={span.key}>
      <b>{span.pages ? t('citation.wiki_source_span', { document: span.document, pages: span.pages }) : span.document}</b>
      {/* txt/md/docx 教材的分片没有页码，那种出处按整份文档标，不写成 p.0。 */}
      <div>{span.items.map(item => <button type="button" key={`${item.material_id ?? ''}:${item.document}:${item.page}`} onClick={() => onOpen(asMaterial(item))}>{typeof item.page === 'number' ? t('citation.page_short', { n: item.page }) : t('citation.whole_document')}</button>)}</div>
    </div>)}
    {total > sources.length && <p className="citation-location">{t('citation.wiki_sources_more', { n: total, m: sources.length })}</p>}
  </div>
}

/** Agent 回复开头的名字。开发者模式开着时它是个按钮，点开这一轮的 trace；
 *  关掉时渲染成普通文本，页面上根本没有那个按钮可点。 */
function AgentLabel({ turnId }: { turnId: string | null }) {
  const { openTrace } = useDevMode()
  const [confirming, setConfirming] = useState(false)
  useEffect(() => { if (!openTrace) setConfirming(false) }, [openTrace])
  const name = <><span aria-hidden>❯</span><b>CoursePilot</b></>
  // 流式中的临时消息还没有 turn_id，那一轮的 trace 也还没写下来。
  if (!openTrace || !turnId) return <div className="agent-label">{name}</div>
  return <div className="agent-label agent-label-dev">
    <button type="button" className="agent-name" title={t('trace.open_hint')} onClick={() => setConfirming(true)}>{name}</button>
    {confirming && <span className="trace-confirm" role="dialog" aria-label={t('trace.confirm_question')}>
      <b>{t('trace.confirm_question')}</b>
      <button type="button" className="trace-confirm-yes" onClick={() => { setConfirming(false); openTrace(turnId) }}>{t('trace.confirm_yes')}</button>
      <button type="button" onClick={() => setConfirming(false)}>{t('common.cancel')}</button>
    </span>}
  </div>
}

function traceMs(value?: number | null) { return typeof value === 'number' ? t('trace.ms', { n: value }) : '—' }

function TraceFacts({ rows }: { rows: [string, ReactNode][] }) {
  return <dl className="trace-facts">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
}

function TraceJson({ value }: { value: unknown }) {
  return <pre className="trace-json">{JSON.stringify(value, null, 2)}</pre>
}

/** 工具正文：点开这一块才去取那一条，打开侧栏时不下载任何正文。 */
function TraceBodyBlock({ sessionId, turnId, body }: { sessionId: string; turnId: string; body: TraceBody }) {
  const [text, setText] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const load = () => {
    if (text !== null || loading) return
    setLoading(true); setError('')
    api.traceBody(sessionId, turnId, body.call_id)
      .then(payload => setText(payload.text ?? ''))
      .catch(problem => setError(errorText(problem)))
      .finally(() => setLoading(false))
  }
  return <details className="trace-body" onToggle={event => { if (event.currentTarget.open) load() }}>
    <summary>{t('trace.tool_body', { n: body.chars })}</summary>
    {loading && <p className="trace-note">{t('trace.body_loading')}</p>}
    {error && <p className="trace-note trace-warn">{error}</p>}
    {text !== null && <pre>{text}</pre>}
  </details>
}

const BODY_STATE_NOTE: Record<string, string> = {
  not_persisted: 'trace.body_not_persisted', reused: 'trace.body_reused',
  denied: 'trace.body_denied', failed: 'trace.body_failed', missing: 'trace.body_missing',
}

function TraceBodyList({ title, bodies, sessionId, turnId }: {
  title: string; bodies: TraceBody[]; sessionId: string; turnId: string
}) {
  if (bodies.length === 0) return null
  return <section><h3>{title}</h3>{bodies.map(body => <div className="trace-loose-body" key={body.call_id}>
    <div className="trace-tool-head"><b>{tOr(`tool.${body.name}`, body.name)}</b><span className="trace-origin">{body.call_id}</span></div>
    <TraceBodyBlock sessionId={sessionId} turnId={turnId} body={body} />
  </div>)}</section>
}

/** 时序里的一次调用：摘要、参数、以及按需展开的正文。 */
function TraceCall({ tool, sessionId, turnId, subagent }: {
  tool: TraceTool; sessionId: string; turnId: string; subagent?: TraceSubagent
}) {
  const summary = tool.summary_key ? tOr(tool.summary_key, tool.summary ?? '', tool.summary_args ?? undefined) : tool.summary
  const note = BODY_STATE_NOTE[tool.body_state]
  return <li>
    <div className="trace-tool-head">
      <b>{tOr(`tool.${tool.name}`, tool.name ?? '—')}</b>
      <span className="trace-origin">{tool.origin ?? '—'}</span>
      <span>{traceMs(tool.duration_ms)}</span>
      {tool.reused && <i className="trace-badge">{t('trace.reused')}</i>}
      {tool.decision === 'denied' && <i className="trace-bad">{t('trace.denied')}</i>}
      {tool.ok === false && <i className="trace-bad">✕</i>}
    </div>
    {summary && <p className="trace-summary">{summary}</p>}
    {tool.reason && <p className="trace-note trace-warn">{tool.reason}</p>}
    {tool.arguments_ref
      ? <p className="trace-note trace-warn">{t('trace.tool_args_dropped', { n: tool.arguments_ref.chars ?? 0 })}</p>
      : <details className="trace-args"><summary>{t('trace.tool_args')}</summary><TraceJson value={tool.arguments ?? {}} /></details>}
    {subagent && <TraceSubagentSteps subagent={subagent} />}
    {tool.body
      ? <TraceBodyBlock sessionId={sessionId} turnId={turnId} body={tool.body} />
      : <p className="trace-note">{tOr(note ?? 'trace.body_missing', '')}</p>}
  </li>
}

/** 子任务在父轮里只有骨架：几轮、每轮多长、调了哪些工具。它查到的正文单列在下面那一栏。 */
function TraceSubagentSteps({ subagent }: { subagent: TraceSubagent }) {
  return <details className="trace-args">
    <summary>{t('trace.subagent_steps', { n: subagent.steps.length })}</summary>
    <ol className="trace-substeps">{subagent.steps.map(step => <li key={step.round}>
      <span>{t('trace.subagent_step', { n: step.round, r: step.reasoning_chars, t: step.text_chars })}</span>
      <b>{step.calls.length > 0 ? step.calls.map(name => tOr(`tool.${name}`, name)).join(t('common.list_sep')) : t('trace.subagent_no_calls')}</b>
    </li>)}</ol>
  </details>
}

/** outcome 是服务端派生的标签，光看枚举名说不清发生了什么，这几个补一句。 */
const OUTCOME_NOTE: Record<string, string> = {
  budget_exhausted: 'trace.outcome_note_budget_exhausted',
  remediation: 'trace.outcome_note_remediation',
  no_response: 'trace.outcome_note_no_response',
}

/** 老 trace 没记字段名，退到 OpenAI 兼容接口上的通用名。 */
function reasoningField(step: TraceStep) { return step.reasoning_field ?? 'reasoning_content' }

/** 一次 chat completion：reasoning_content → assistant.content → tool_calls。思考很长，默认收起。
 *  finish_reason 是厂商说的、outcome 是我们判的，两个 chip 分开摆，别让人以为是同一件事。 */
function TraceStepCard({ step, calls, sessionId, turnId, subagents }: {
  step: TraceStep; calls: TraceTool[]; sessionId: string; turnId: string; subagents: TraceSubagent[]
}) {
  const outcomeNote = step.outcome ? OUTCOME_NOTE[step.outcome] : undefined
  return <li className="trace-step">
    <div className="trace-step-head">
      <b>{t('trace.flow_step', { n: step.round })}</b>
      <i className="trace-chip-provider" title={t('trace.hint_finish_reason')}>
        {t('trace.chip_finish_reason', { v: step.finish_reason ?? 'null' })}</i>
      {step.outcome && <i className="trace-chip-server" title={t('trace.hint_outcome')}>
        {t('trace.chip_outcome', { v: step.outcome })}</i>}
      {step.injected && <i className="trace-chip-server" title={t('trace.hint_injected')}>
        {t('trace.chip_injected', { v: step.injected })}</i>}
    </div>
    {step.injected && <p className="trace-note">{tOr(`trace.injected_${step.injected}`, '')}</p>}
    {outcomeNote && <p className="trace-note">{tOr(outcomeNote, '')}</p>}
    {step.reasoning !== null
      ? <details className="trace-thinking"><summary>{t('trace.reasoning', { field: reasoningField(step), n: step.reasoning_chars })}</summary><pre>{step.reasoning}</pre></details>
      : step.reasoning_chars > 0 && <p className="trace-note trace-warn">{t('trace.reasoning_missing', { field: reasoningField(step), n: step.reasoning_chars })}</p>}
    {step.text !== null && <div className="trace-step-text"><h4>{t('trace.step_content')}</h4><pre>{step.text}</pre></div>}
    {step.text === null && step.text_chars > 0 && <p className="trace-note trace-warn">{t('trace.step_content_missing', { n: step.text_chars })}</p>}
    {step.text === null && step.text_chars === 0 && <p className="trace-note">{t('trace.step_content_empty')}</p>}
    {calls.length > 0 && <ol className="trace-tools">{calls.map(tool => (
      <TraceCall key={tool.index} tool={tool} sessionId={sessionId} turnId={turnId}
        subagent={subagents.find(item => item.call_id === tool.call_id)} />
    ))}</ol>}
  </li>
}

/** 执行流程：开场的系统动作 → 每一次模型调用 → 最终回答。侧栏第一眼看的就是这一段。 */
function TraceFlow({ turn, sessionId }: { turn: TraceTurn; sessionId: string }) {
  const react = turn.react
  const claimed = new Set(react.steps.flatMap(step => step.calls))
  const prelude = turn.tools.filter(tool => tool.round === 0)
  // 老 trace 的 span 没有 round，模型那几次也归不到某一步：单列在时序末尾，别把它们藏掉
  const loose = turn.tools.filter(tool => tool.round !== 0 && !claimed.has(tool.call_id))
  const byCallId = new Map(turn.tools.map(tool => [tool.call_id, tool]))
  const render = (tool: TraceTool) => <TraceCall key={tool.index} tool={tool} sessionId={sessionId}
    turnId={turn.turn_id} subagent={react.subagents.find(item => item.call_id === tool.call_id)} />

  if (react.steps.length === 0 && turn.tools.length === 0) {
    return <section><h3>{t('trace.section_flow')}</h3><p className="trace-note">{t('trace.flow_empty')}</p></section>
  }
  return <section><h3>{t('trace.section_flow')}</h3>
    {react.dropped_chars > 0 && <p className="trace-note trace-warn">{t('trace.flow_dropped', { n: react.dropped_chars })}</p>}
    <ol className="trace-steps">
      {prelude.length > 0 && <li className="trace-step">
        <div className="trace-step-head"><b>{t('trace.flow_prelude')}</b></div>
        <ol className="trace-tools">{prelude.map(render)}</ol>
      </li>}
      {react.steps.map(step => <TraceStepCard key={step.round} step={step} sessionId={sessionId} turnId={turn.turn_id}
        subagents={react.subagents}
        calls={step.calls.map(id => byCallId.get(id)).filter((tool): tool is TraceTool => Boolean(tool))} />)}
      {loose.length > 0 && <li className="trace-step">
        <div className="trace-step-head"><b>{t('trace.section_tools')}</b></div>
        <ol className="trace-tools">{loose.map(render)}</ol>
      </li>}
    </ol>
    {react.answer !== null && <details className="trace-answer" open>
      <summary>{t('trace.flow_answer', { n: react.answer_chars })}</summary><pre>{react.answer}</pre>
    </details>}
    {react.answer === null && react.answer_chars > 0 && <p className="trace-note trace-warn">{t('trace.flow_answer_missing', { n: react.answer_chars })}</p>}
  </section>
}

const PAYLOAD_NOTE: Record<string, string> = {
  missing: 'trace.payload_missing', invalid: 'trace.payload_invalid',
  oversized: 'trace.payload_oversized', skipped: 'trace.payload_skipped',
}

/** 一轮的卡片。执行流程在最上面，课程判定、用量这些统计折到最后并默认收起。 */
function TraceTurnCard({ turn, ordinal, focused, sessionId, onFocus }: {
  turn: TraceTurn; ordinal: number; focused: boolean; sessionId: string; onFocus: () => void
}) {
  const head = <button type="button" className="trace-turn-head" onClick={onFocus}>
    <b>{t('trace.turn_label', { n: ordinal })}</b>
    <span>{turn.started_at ? timeLabel(turn.started_at) : '—'}</span>
    <span className="trace-status">{turn.error_code ?? turn.status ?? '—'}</span>
    <span>{traceMs(turn.duration_ms)}</span>
    {focused && <i className="trace-badge">{t('trace.focus_badge')}</i>}
  </button>
  if (!focused) return <div className="trace-turn">{head}</div>
  const payloadNote = PAYLOAD_NOTE[turn.payload_state]
  return <div className="trace-turn focused">{head}
    {!turn.trace_record && <p className="trace-note trace-warn">{t('trace.no_record')}</p>}
    {payloadNote && <p className="trace-note trace-warn">{tOr(payloadNote, '')}</p>}
    <TraceFlow turn={turn} sessionId={sessionId} />
    <TraceBodyList title={t('trace.section_subagent')} bodies={turn.subagent_bodies} sessionId={sessionId} turnId={turn.turn_id} />
    <TraceBodyList title={t('trace.section_unmatched')} bodies={turn.unmatched_bodies} sessionId={sessionId} turnId={turn.turn_id} />
    {turn.trace_record && <details className="trace-stats"><summary>{t('trace.section_stats')}</summary>
      <section><h3>{t('trace.section_basics')}</h3><TraceFacts rows={[
        [t('trace.field_turn'), <code key="id">{turn.turn_id}</code>],
        [t('trace.field_started'), turn.started_at ?? '—'],
        [t('trace.field_status'), turn.error_code ? `${turn.status ?? '—'} · ${turn.error_code}` : turn.status ?? '—'],
        [t('trace.field_duration'), traceMs(turn.duration_ms)],
        [t('trace.field_prompt'), turn.prompt_version ?? '—'],
        [t('trace.field_scope'), turn.scope_mode ?? '—'],
        [t('trace.field_answer_chars'), turn.answer_chars ?? '—'],
        [t('trace.field_citations'), `${turn.citations ?? '—'} / ${turn.citations_retrieved ?? '—'}`],
        [t('trace.field_tool_rounds'), turn.tool_rounds ?? '—'],
      ]} /></section>
      {turn.resolution && <section><h3>{t('trace.section_resolution')}</h3><TraceJson value={turn.resolution} /></section>}
      {turn.responder && <section><h3>{t('trace.section_responder')}</h3><TraceJson value={turn.responder} /></section>}
      {turn.usage && <section><h3>{t('trace.section_usage')}</h3><TraceJson value={turn.usage} /></section>}
      {Object.keys(turn.extras).length > 0 && <section><h3>{t('trace.section_extras')}</h3><TraceJson value={turn.extras} /></section>}
    </details>}
  </div>
}

/** 开发者模式侧栏：整个会话的轮次，点中的那一轮展开。
 *  这里只做观测。trace 可以随时被清理，读不到就照实说，不要在界面上补出一份来。 */
function TraceDrawer({ sessionId, turnId, onFocus, onClose }: {
  sessionId: string; turnId: string; onFocus: (turnId: string) => void; onClose: () => void
}) {
  const box = useDismiss(onClose)
  const [data, setData] = useState<SessionTrace | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    let alive = true
    setLoading(true); setError('')
    api.sessionTrace(sessionId, turnId)
      .then(payload => { if (alive) setData(payload) })
      .catch(problem => { if (alive) { setData(null); setError(errorText(problem)) } })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [sessionId, turnId])

  return <aside ref={box} className="trace-drawer" role="dialog" aria-label={t('a11y.trace_drawer')}>
    <header>
      <div><p>{t('trace.title')}</p><h2>{t('trace.subtitle')}</h2></div>
      <button aria-label={t('a11y.close_trace')} onClick={onClose}>×</button>
    </header>
    {loading && <p className="trace-note">{t('trace.loading')}</p>}
    {error && <p className="trace-note trace-warn">{error}</p>}
    {data && <>
      {/* 覆盖不全与点不到那一轮是数据缺失，留在最上面；纯统计的扫描量折到最后 */}
      {data.scan.truncated && <p className="trace-note trace-warn">{t('trace.truncated', { turns: data.limits.max_turns, lines: data.limits.max_scan_lines, files: data.limits.max_day_files })}</p>}
      {!data.focus_found && data.turns.length > 0 && <p className="trace-note trace-warn">{t('trace.focus_missing')}</p>}
      {data.turns.length === 0 && <p className="trace-empty">{t('trace.empty')}</p>}
      {/* key 带上序号：同一份 JSONL 里出现两条相同 turn_id 时 turn_id 单独做 key 会撞 */}
      <ol className="trace-turns">{data.turns.map((turn, index) => <li key={`${index}:${turn.turn_id}`}>
        <TraceTurnCard turn={turn} ordinal={index + 1} focused={turn.turn_id === data.focus_turn_id}
          sessionId={sessionId} onFocus={() => onFocus(turn.turn_id)} />
      </li>)}</ol>
      <p className="trace-note">{t('trace.scan_note', { files: data.scan.files.length, lines: data.scan.scanned_lines })}</p>
    </>}
  </aside>
}

function CitationDrawer({ citation, onClose, onOpen }: { citation: Citation; onClose: () => void; onOpen: (citation: Citation) => void }) {
  const isWiki = citation.kind === 'wiki'
  // 抽屉头部就要说清这是转述稿：正文没有页码，用户不该以为自己在看教材原文。
  const heading = isWiki ? (citation.concept_name || citation.concept_id || t('citation.wiki_fallback')) : (citation.material_name ?? t('citation.fallback_name'))
  // 没有页码的教材（txt/md/docx）按整份文档说，和出处列表上那颗按钮口径一致；
  // 连文档名都没有时才退回分片 id，那是唯一还剩的定位。
  const location = isWiki ? t('citation.wiki_location')
    : citation.page ? t('citation.page', { n: citation.page })
    : citation.material_name ? t('citation.whole_document')
    : citation.chunk_id ? t('citation.chunk', { id: citation.chunk_id }) : t('citation.location_unknown')
  const box = useDismiss(onClose)
  // 原文片段可能是空串（分片被重建索引换掉时服务端就返回空），空串按没有原文处理。
  return <aside ref={box} className="citation-drawer" role="dialog" aria-label={t('a11y.citation_drawer')}><header><div><p>{isWiki ? t('citation.wiki_title') : t('citation.title')}</p><h2>{heading}</h2></div><button aria-label={t('a11y.close_citation')} onClick={onClose}>×</button></header><p className="citation-location">{location}</p><blockquote>{citation.text || t('citation.no_text')}</blockquote>{isWiki && <WikiSources citation={citation} onOpen={onOpen} />}{citation.score !== undefined && <p>{t('citation.score', { score: citation.score.toFixed(4) })}</p>}</aside>
}
