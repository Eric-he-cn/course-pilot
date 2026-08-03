import { t } from './i18n'
import type { ArchiveSummary, Attachment, Citation, ConceptNode, Course, Job, Material, MaterialStructure, McpOverview, Message, OcrEstimate, Plan, SearchResult, SessionTrace, SessionSummary, ScopeMode, NoteSummary, SkillInfo, StructurePreview, TraceBodyText, TurnEvent, WikiEstimate, WikiPageSummary } from './types'

const BASE = import.meta.env.VITE_API_BASE ?? '/api/v2'
const USER_KEY = 'cp-username'
const MODEL_KEY = 'cp-model'
const THINKING_KEY = 'cp-thinking'
const DEVMODE_KEY = 'cp-devmode'

export function currentUser(): string { return localStorage.getItem(USER_KEY) ?? '' }
export function setCurrentUser(name: string) { localStorage.setItem(USER_KEY, name) }
export function clearCurrentUser() { localStorage.removeItem(USER_KEY) }

/** 模型与思考开关存在本地：服务端保持无状态，多个标签页可以各用各的。
 *  没选过就不带这两个头，由服务端用配置里的第一个模型与它的默认值。 */
export function currentModel(): string { return localStorage.getItem(MODEL_KEY) ?? '' }
export function setCurrentModel(key: string) { localStorage.setItem(MODEL_KEY, key) }
/** 思考档位：off / adaptive / high / max，与后端 THINKING_TIERS 对应。 */
export function currentThinking(): string { return localStorage.getItem(THINKING_KEY) ?? '' }
export function setCurrentThinking(tier: string) { localStorage.setItem(THINKING_KEY, tier) }

/** 开发者模式：纯客户端的显示开关，只决定要不要把 trace 入口摆出来。存本地，不进库。 */
export function currentDevMode(): boolean { return localStorage.getItem(DEVMODE_KEY) === 'on' }
export function setCurrentDevMode(on: boolean) { localStorage.setItem(DEVMODE_KEY, on ? 'on' : 'off') }

function modelHeaders(): Record<string, string> {
  const headers: Record<string, string> = {}
  const model = currentModel()
  if (model) headers['X-CoursePilot-Model'] = model
  const thinking = currentThinking()
  if (thinking) headers['X-CoursePilot-Thinking'] = thinking
  return headers
}

/** HTTP 头值是 ByteString：中日韩用户名必须编码后再放，否则浏览器 fetch 直接抛 TypeError。 */
function userHeaders(): Record<string, string> {
  const name = currentUser()
  return name ? { 'X-CoursePilot-User': encodeURIComponent(name) } : {}
}
type BackendCitation = Citation & { citation_id?: string; document?: string; snippet?: string }

export class ApiError extends Error {
  constructor(message: string, public status?: number) { super(message) }
}

/** 只报告掉线，不报告在线：在线由 health 心跳判定。
 *  开发时前端走 vite 代理，后端挂了代理会返回 500 而不是让 fetch 抛错——
 *  把「拿到响应」当成在线，掉线就永远发现不了。 */
let reportOffline: (() => void) | null = null
export function onConnectionLost(fn: () => void) { reportOffline = fn }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, { ...init, headers: { ...(init?.headers ?? {}), ...userHeaders() } })
  } catch {
    reportOffline?.()
    throw new ApiError(t('api.offline'))
  }
  if (!response.ok) {
    let detail = ''
    let code = ''
    try {
      const body = await response.json() as {
        detail?: string | { message?: string; error?: { message?: string; code?: string } }
        error?: { message?: string; code?: string }
      }
      detail = typeof body.detail === 'string' ? body.detail : body.detail?.message ?? body.detail?.error?.message ?? ''
      // 错误处理器把 error 提到顶层、detail 留成消息串；少数端点直接抛嵌套形式，两种都认。
      code = body.error?.code ?? (typeof body.detail === 'object' ? body.detail?.error?.code ?? '' : '')
    } catch { /* response is not JSON */ }
    // 本地存了个规则变严后不再合法的名字：清掉，让界面回到登录页。
    // 认错误码而不是 message 里的文字，后端措辞变了或翻译了都不影响。
    if (response.status === 422 && code === 'invalid_username') {
      clearCurrentUser()
    }
    throw new ApiError(detail || t('api.request_failed', { status: response.status }), response.status)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
})

export const api = {
  health: () => request<Record<string, unknown>>('/health'),
  courses: () => request<Course[]>('/courses'),
  createCourse: (name: string) => request<Course>('/courses', json('POST', { name })),
  updateCourse: (id: string, patch: Partial<Course>) => request<Course>(`/courses/${id}`, json('PATCH', patch)),
  sessions: (scope: ScopeMode, courseId?: string) => {
    const params = new URLSearchParams({ scope_mode: scope })
    if (courseId) params.set('course_id', courseId)
    return request<SessionSummary[]>(`/sessions?${params}`)
  },
  createSession: (scope: ScopeMode, courseId?: string) => request<SessionSummary>('/sessions', json('POST', { scope_mode: scope, course_id: courseId ?? null })),
  renameSession: (sessionId: string, title: string) => request<SessionSummary>(`/sessions/${sessionId}`, json('PATCH', { title })),
  deleteSession: (sessionId: string) => request<void>(`/sessions/${sessionId}`, { method: 'DELETE' }),
  deleteCourse: (courseId: string) => request<void>(`/courses/${courseId}`, { method: 'DELETE' }),
  deleteMaterial: (materialId: string) => request<void>(`/materials/${materialId}`, { method: 'DELETE' }),
  messages: async (sessionId: string) => {
    const payload = await request<{ messages: Array<Omit<Message, 'citations'> & { citations?: BackendCitation[] }> }>(`/sessions/${sessionId}/messages`)
    // 工具正文（role='tool'）是给模型跨轮读回的资料，不是对话的一句话。服务端已经滤过，
    // 这里再滤一次：多一道拦截比事后发现检索原文被画成气泡便宜。
    return payload.messages.filter(message => message.role !== 'tool').map(message => ({ ...message, citations: message.citations?.map(citation => ({ ...citation, id: citation.id ?? citation.citation_id, material_name: citation.material_name ?? citation.document, text: citation.text ?? citation.snippet })) }))
  },
  /** 开发者模式侧栏：整个会话的轮次，turnId 指出高亮哪一轮。只有时序与长度，不含工具正文。 */
  sessionTrace: (sessionId: string, turnId?: string) => {
    const query = turnId ? `?${new URLSearchParams({ turn_id: turnId })}` : ''
    return request<SessionTrace>(`/sessions/${sessionId}/trace${query}`)
  },
  /** 某一步取回的正文，点开那一步才来取。 */
  traceBody: (sessionId: string, turnId: string, callId: string) => {
    const query = new URLSearchParams({ turn_id: turnId, call_id: callId })
    return request<TraceBodyText>(`/sessions/${sessionId}/trace/body?${query}`)
  },
  materials: (courseId: string) => request<Material[]>(`/courses/${courseId}/materials`),
  uploadMaterial: (courseId: string, file: File) => {
    const body = new FormData(); body.set('file', file)
    return request<Material>(`/courses/${courseId}/materials`, { method: 'POST', body })
  },
  uploadAttachment: (sessionId: string, file: File) => {
    const body = new FormData(); body.set('file', file)
    return request<Attachment>(`/sessions/${sessionId}/attachments`, { method: 'POST', body })
  },
  skills: () => request<{ skills: SkillInfo[]; importable_tools: string[] }>('/skills'),
  importSkill: (files: File[]) => {
    const body = new FormData()
    // 选目录时相对路径只在 webkitRelativePath 上，得显式当文件名传，服务端才知道 SKILL.md 在哪一层
    for (const file of files) body.append('file', file, file.webkitRelativePath || file.name)
    return request<SkillInfo & { skipped_files?: string[] }>('/skills', { method: 'POST', body })
  },
  setSkillEnabled: (name: string, enabled: boolean) => request<{ name: string; status: string }>(`/skills/${name}`, json('PATCH', { enabled })),
  deleteSkill: (name: string) => request<void>(`/skills/${name}`, { method: 'DELETE' }),
  mcpServers: () => request<McpOverview>('/mcp/servers'),
  // credential 只往上传，读接口从不带它回来。
  addMcpServer: (body: { label: string; url: string; credential?: string; note?: string }) =>
    request<McpOverview>('/mcp/servers', json('POST', body)),
  connectMcpServer: (id: string, credential?: string) =>
    request<McpOverview>(`/mcp/servers/${id}/connect`, json('POST', credential === undefined ? {} : { credential })),
  setMcpEnabled: (id: string, enabled: boolean) => request<McpOverview>(`/mcp/servers/${id}`, json('PATCH', { enabled })),
  deleteMcpServer: (id: string) => request<void>(`/mcp/servers/${id}`, { method: 'DELETE' }),
  indexMaterial: (materialId: string) => request<Job>(`/materials/${materialId}/index`, json('POST')),
  estimateOcr: (materialId: string) => request<OcrEstimate>(`/materials/${materialId}/ocr/estimate`, json('POST')),
  startOcr: (materialId: string) => request<Job>(`/materials/${materialId}/ocr`, json('POST')),
  job: (jobId: string) => request<Job>(`/jobs/${jobId}`),
  buildWiki: (materialId: string) => request<Job>(`/materials/${materialId}/wiki`, json('POST')),
  estimateWiki: (materialId: string) => request<WikiEstimate>(`/materials/${materialId}/wiki/estimate`),
  concepts: (courseId: string) => request<{ concepts: ConceptNode[] }>(`/courses/${courseId}/concepts`),
  structure: (courseId: string) => request<{ materials: MaterialStructure[] }>(`/courses/${courseId}/structure`),
  previewStructure: (materialId: string) => request<StructurePreview>(`/materials/${materialId}/structure/preview`, json('POST')),
  parseStructure: (materialId: string) => request<StructurePreview & MaterialStructure>(`/materials/${materialId}/structure`, json('POST')),
  wikiPages: (courseId: string) => request<{ pages: WikiPageSummary[] }>(`/courses/${courseId}/wiki`),
  wikiPage: (courseId: string, conceptId: string) => request<{ concept_id: string; content: string }>(`/courses/${courseId}/wiki/${conceptId}`),
  search: (courseId: string, query: string) => request<SearchResult[]>(`/courses/${courseId}/knowledge/search`, json('POST', { query })),
  plan: (courseId: string) => request<{ plan: Plan | null }>(`/courses/${courseId}/plan`),
  archive: (courseId: string) => request<ArchiveSummary>(`/courses/${courseId}/archive`),
  memory: (courseId?: string) => request<{ scope: string; content: string }>(courseId ? `/courses/${courseId}/memory` : '/memory'),
  saveMemory: (content: string, courseId?: string) => request<{ scope: string; content: string }>(courseId ? `/courses/${courseId}/memory` : '/memory', json('PUT', { content })),
  notes: (courseId: string) => request<{ notes: NoteSummary[] }>(`/courses/${courseId}/notes`),
  note: (courseId: string, title: string) => request<{ title: string; content: string }>(`/courses/${courseId}/notes/${encodeURIComponent(title)}`),
  async turn(sessionId: string, content: string, onEvent: (payload: TurnEvent) => void, attachmentIds: string[] = [], signal?: AbortSignal): Promise<void> {
    let response: Response
    try {
      const init = json('POST', { message: content, attachment_ids: attachmentIds })
      response = await fetch(`${BASE}/sessions/${sessionId}/turns`, { ...init, headers: { ...init.headers, ...userHeaders(), ...modelHeaders() }, signal })
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') throw error
      throw new ApiError(t('api.offline'))
    }
    if (!response.ok || !response.body) {
      let detail = ''
      try {
        const body = await response.json() as { detail?: string | { message?: string; error?: { message?: string } } }
        detail = typeof body.detail === 'string' ? body.detail : body.detail?.message ?? body.detail?.error?.message ?? ''
      } catch { /* no JSON detail */ }
      throw new ApiError(detail || t('api.send_failed', { status: response.status }), response.status)
    }
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''
    // 用户点停止：中断读取即可，服务端的 finally 会把这一轮落成终态，
    // 已生成的内容仍在库里，上层回读消息就能拿到。
    signal?.addEventListener('abort', () => void reader.cancel().catch(() => {}), { once: true })
    while (true) {
      const { done, value } = await reader.read(); if (done) break
      buffer += decoder.decode(value, { stream: true })
      const events = buffer.split('\n\n'); buffer = events.pop() ?? ''
      for (const event of events) {
        const eventName = event.split('\n').find(line => line.startsWith('event:'))?.slice(6).trim()
        const data = event.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('')
        if (!data || data === '[DONE]') continue
        try {
          const payload = JSON.parse(data) as TurnEvent
          if (payload.error) throw new ApiError(payload.error)
          if (eventName === 'turn_failed') {
            const message = payload.error_code === 'session_busy' ? t('api.session_busy')
              : payload.error_code === 'stream_interrupted' ? t('api.stream_interrupted')
              : payload.error_code === 'attachment_not_found' ? t('api.attachment_not_found')
              : t('api.turn_failed')
            throw new ApiError(message)
          }
          onEvent({ ...payload, event: eventName, type: eventName })
        } catch (error) {
          if (error instanceof ApiError) throw error
          onEvent({ delta: data })
        }
      }
    }
  },
}
