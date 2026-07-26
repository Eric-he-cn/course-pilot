import type { ArchiveSummary, Attachment, Citation, Course, Job, Material, Message, Plan, SearchResult, SessionSummary, ScopeMode, NoteSummary, SkillInfo, TurnEvent } from './types'

const BASE = import.meta.env.VITE_API_BASE ?? '/api/v2'
const USER_KEY = 'cp-username'
const MODEL_KEY = 'cp-model'
const THINKING_KEY = 'cp-thinking'

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
    throw new ApiError('无法连接 CoursePilot 服务。请确认后端已启动。')
  }
  if (!response.ok) {
    let detail = ''
    try {
      const body = await response.json() as { detail?: string | { message?: string; error?: { message?: string } } }
      detail = typeof body.detail === 'string' ? body.detail : body.detail?.message ?? body.detail?.error?.message ?? ''
    } catch { /* response is not JSON */ }
    if (response.status === 422 && detail.includes('用户名')) {
      // 本地存了个规则变严后不再合法的名字：清掉，让界面回到登录页。
      clearCurrentUser()
    }
    throw new ApiError(detail || `请求失败（${response.status}）`, response.status)
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
    return payload.messages.map(message => ({ ...message, citations: message.citations?.map(citation => ({ ...citation, id: citation.id ?? citation.citation_id, material_name: citation.material_name ?? citation.document, text: citation.text ?? citation.snippet })) }))
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
  indexMaterial: (materialId: string) => request<Job>(`/materials/${materialId}/index`, json('POST')),
  job: (jobId: string) => request<Job>(`/jobs/${jobId}`),
  buildWiki: (materialId: string) => request<Job>(`/materials/${materialId}/wiki`, json('POST')),
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
      throw new ApiError('无法连接 CoursePilot 服务。请确认后端已启动。')
    }
    if (!response.ok || !response.body) {
      let detail = ''
      try {
        const body = await response.json() as { detail?: string | { message?: string; error?: { message?: string } } }
        detail = typeof body.detail === 'string' ? body.detail : body.detail?.message ?? body.detail?.error?.message ?? ''
      } catch { /* no JSON detail */ }
      throw new ApiError(detail || `发送失败（${response.status}）`, response.status)
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
            const message = payload.error_code === 'session_busy' ? '该会话正在生成回答，请稍后重试。'
              : payload.error_code === 'stream_interrupted' ? '回答在生成中被中断，已生成的内容已保留。'
              : payload.error_code === 'attachment_not_found' ? '图片附件无效或不属于当前会话，请重新上传。'
              : '本次回答未能完成，请重试。'
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
