import type { ArchiveSummary, Attachment, Citation, Course, Job, Material, Message, Plan, SearchResult, SessionSummary, ScopeMode, SkillInfo, TurnEvent } from './types'

const BASE = import.meta.env.VITE_API_BASE ?? '/api/v2'
type BackendCitation = Citation & { citation_id?: string; document?: string; snippet?: string }

export class ApiError extends Error {
  constructor(message: string, public status?: number) { super(message) }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, init)
  } catch {
    throw new ApiError('无法连接 CoursePilot 服务。请确认后端已启动。')
  }
  if (!response.ok) {
    let detail = ''
    try {
      const body = await response.json() as { detail?: string | { message?: string; error?: { message?: string } } }
      detail = typeof body.detail === 'string' ? body.detail : body.detail?.message ?? body.detail?.error?.message ?? ''
    } catch { /* response is not JSON */ }
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
  importSkill: (file: File) => {
    const body = new FormData(); body.set('file', file)
    return request<SkillInfo>('/skills', { method: 'POST', body })
  },
  setSkillEnabled: (name: string, enabled: boolean) => request<{ name: string; status: string }>(`/skills/${name}`, json('PATCH', { enabled })),
  deleteSkill: (name: string) => request<void>(`/skills/${name}`, { method: 'DELETE' }),
  indexMaterial: (materialId: string) => request<Job>(`/materials/${materialId}/index`, json('POST')),
  job: (jobId: string) => request<Job>(`/jobs/${jobId}`),
  buildWiki: (materialId: string) => request<Job>(`/materials/${materialId}/wiki`, json('POST')),
  search: (courseId: string, query: string) => request<SearchResult[]>(`/courses/${courseId}/knowledge/search`, json('POST', { query })),
  plan: (courseId: string) => request<{ plan: Plan | null }>(`/courses/${courseId}/plan`),
  archive: (courseId: string) => request<ArchiveSummary>(`/courses/${courseId}/archive`),
  async turn(sessionId: string, content: string, onEvent: (payload: TurnEvent) => void, attachmentIds: string[] = []): Promise<void> {
    let response: Response
    try {
      response = await fetch(`${BASE}/sessions/${sessionId}/turns`, json('POST', { message: content, attachment_ids: attachmentIds }))
    } catch { throw new ApiError('无法连接 CoursePilot 服务。请确认后端已启动。') }
    if (!response.ok || !response.body) {
      let detail = ''
      try {
        const body = await response.json() as { detail?: string | { message?: string; error?: { message?: string } } }
        detail = typeof body.detail === 'string' ? body.detail : body.detail?.message ?? body.detail?.error?.message ?? ''
      } catch { /* no JSON detail */ }
      throw new ApiError(detail || `发送失败（${response.status}）`, response.status)
    }
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''
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
