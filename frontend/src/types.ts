export type ScopeMode = 'general' | 'course'

export interface Course {
  id: string
  name: string
  color: string
  wiki_enabled?: boolean
}

export interface SessionSummary {
  id: string
  title: string
  scope_mode: ScopeMode
  course_id: string | null
  resolved_course_id: string | null
  course_name?: string | null
  course_color?: string | null
  source?: string
  updated_at: string
}

export interface Citation {
  id?: string
  // 服务端给出的引用编号，与回答正文里的 [n] 对应
  number?: number
  // 来源类别显式给出：网页点开是外链，教材与知识页点开是抽屉。
  // 不靠「有没有 url」这类隐式判断——那会在字段缺失时静默走错分支。
  // wiki 是系统按教材整理的转述稿，没有页码，界面必须和教材原文标得不一样。
  kind?: 'material' | 'web' | 'wiki'
  url?: string
  title?: string
  material_id?: string
  material_name?: string
  concept_id?: string
  concept_name?: string
  page?: number | null
  chunk_id?: string
  text?: string
  score?: number
}

export interface ToolActivity {
  call_id: string
  name: string
  origin?: string
  summary?: string
  // summary 是后端给的中文串，认识 key 就用本地译文。老数据没有 key，只能用 summary。
  summary_key?: string
  summary_args?: Record<string, string | number>
  // 同一轮里重复调用被复用：后缀由前端拼，后端不把中文摘要当参数传
  reused?: boolean
  ok?: boolean
  // 仅流式期间有值：用于显示"这一步已经跑了多久"
  started_at?: number
  elapsed_ms?: number
}

export interface SkillInfo {
  name: string
  description: string
  when_to_use: string
  allowed_tools: string[]
  denied_tools: string[]
  origin: 'builtin' | 'user'
  status: 'enabled' | 'draft' | 'permission_denied'
  content_hash: string
  examples?: string[]
}

export interface NoteSummary {
  title: string
  chars: number
  updated_at: string
}

export interface ContextUsage {
  segments: { label: string; label_key?: string; tokens: number }[]
  total_tokens: number
  limit_tokens: number
  history_budget_tokens: number
  dropped_history: number
  clipped_history: number
  compacted_messages: number
}

/** SSE 事件负载：字段随事件类型而异，所以都是可选的。 */
export interface TurnEvent extends Partial<ContextUsage> {
  type?: string
  event?: string
  status?: string
  delta?: string
  content?: string
  text?: string
  error?: string
  error_code?: string
  resolved_course_id?: string | null
  course_id?: string | null
  course_name?: string | null
  course_color?: string | null
  call_id?: string
  name?: string
  summary?: string
  summary_key?: string
  summary_args?: Record<string, string | number>
  ok?: boolean
  origin?: string
  finish_reason?: string
  responder_mode?: string
  provider?: string
  model?: string
  /** ask_user 给出的选项 */
  options?: string[]
}

export interface Message {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  status?: string
  created_at?: string
  citations?: Citation[]
  resolution_status?: 'resolved' | 'ambiguous' | 'unresolved' | null
  resolved_course_id?: string | null
  resolved_course_name?: string | null
  resolved_course_color?: string | null
  // 本轮"查了什么"：流式期间来自 SSE，回读消息后来自服务端持久化记录。
  activity?: ToolActivity[]
  artifact?: { kind: string; visibility?: string; payload?: unknown }
  /** 反问给出的选项：点一下等于把这句话作为新的用户消息发出去 */
  choices?: string[]
  // 本轮切到本地兜底模型的说明；有值就必须显示，否则降级回答会被当成正常回答
  degraded?: string
}

export interface Attachment {
  id: string
  session_id: string
  filename: string
  transcription: string
  needs_confirmation: boolean
  provider?: string
  model?: string
}

export interface Material {
  id: string
  filename?: string
  name?: string
  content_type?: string
  size_bytes?: number
  pages?: number | null
  status?: string
  index_status?: string
  indexed_at?: string | null
  error?: string | null
  chunk_count?: number
  embedded_count?: number
  ocr_approved?: boolean
}

// OCR 估算：取样是实测的，projected 是按页数外推的
export interface OcrEstimate {
  pages: number
  sampled_pages: number
  sample_prompt_tokens: number
  sample_completion_tokens: number
  sample_seconds: number
  projected_prompt_tokens: number
  projected_completion_tokens: number
  projected_total_tokens: number
  projected_seconds?: number
  projected_minutes: number
}

// 概念目录的一条。parent_id / level 来自教材自带的目录书签，没有书签就都是 null。
export interface ConceptNode {
  id: string
  name: string
  page: number | null
  level: number | null
  parent_id: string | null
  material_id: string | null
}

// Wiki 知识页：一个概念一页
export interface WikiPageSummary {
  concept_id: string
  concept_name: string
  updated_at: string
  chars: number
}

export interface Job {
  id: string
  material_id?: string
  course_id?: string
  type?: string
  stage?: string
  status: string
  progress?: number
  error?: string | null
}

export interface SearchResult extends Citation {
  course_id?: string
  course_name?: string
}

export interface PlanItem {
  id: string
  due_date: string
  title: string
  status: string
  concept_id?: string | null
  concept_name?: string | null
}

export interface Plan {
  id: string
  course_id: string
  status: string
  version: number
  updated_at: string
  items: PlanItem[]
}

export interface EvidenceEvent {
  concept_name?: string | null
  id: string
  kind: string
  concept_id?: string | null
  attribution_status?: string
  topic_hint?: string | null
  created_at: string
}

export interface ConceptMastery {
  concept_id: string
  name: string
  // 服务端算出的复合掌握度；null 表示可归因客观证据不足，界面显示"数据不足"
  score: number | null
  objective_events: number
  due_at?: string | null
  insufficient_evidence: boolean
  algorithm_version: string
}

export interface UnattributedTopic {
  topic_hint: string
  hits: number
  last_seen: string
}

export interface MistakeRecord {
  concept_id: string
  name: string
  status: 'active' | 'graduated'
  // 累计错次，毕业不清零；streak 是当前连对次数，答错归零
  wrong_count: number
  streak: number
  first_wrong_at: string
  last_wrong_at: string
  graduated_at?: string | null
  relapse_count: number
}

export interface ArchiveSummary {
  course_id: string
  evidence_count: number
  events: EvidenceEvent[]
  mastery: ConceptMastery[]
  unattributed: UnattributedTopic[]
  // mistakes 只是一页（活跃优先），active_count/graduated_count 才是总数
  mistakes: MistakeRecord[]
  active_count: number
  graduated_count: number
  // 连对几次算清掉：跟着响应下发，前端不另存一份常量
  graduate_streak: number
}
