export type ScopeMode = 'general' | 'course'

export interface Course {
  id: string
  name: string
  color: string
  wiki_enabled?: boolean
  archived?: boolean
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
  material_id?: string
  material_name?: string
  page?: number | null
  chunk_id?: string
  text?: string
  score?: number
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
  artifact?: { kind: string; visibility?: string; payload?: unknown }
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
}

export interface Plan {
  id: string
  course_id: string
  status: string
  version: number
  items: PlanItem[]
}

export interface EvidenceEvent {
  id: string
  kind: string
  concept_id?: string | null
  attribution_status?: string
  topic_hint?: string | null
  created_at: string
}

export interface ArchiveSummary {
  course_id: string
  evidence_count: number
  events: EvidenceEvent[]
}
