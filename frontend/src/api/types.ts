/**
 * Types transcribed from the live API responses, not from the docs.
 *
 * Several docs describe fields the routes do not return, so every shape here was read off
 * a running server. Where the API nests (`source.processing.status`) the type nests too:
 * flattening it here would just move the mismatch into the components.
 */

export interface Creator {
  id: string
  display_name: string | null
}

export interface ProcessingState {
  status: string
  desired_level: number
  achieved_level: number
  current_processing_run_id: string | null
  last_success_at_ms: number | null
  // A source can be fully processed and still contribute nothing to answers, because the
  // Processing Policy hides it. `excluded` is the API's own verdict, not a value derived
  // from `policy_action` in the client, so the two cannot drift apart.
  policy_action: string | null
  excluded: boolean
}

export interface PolicyStatus {
  action: string
  decision_id: string | null
  reason_code: string | null
  phase: string | null
  rule_id: string | null
  rule_name: string | null
  decided_at_ms: number | null
}

export interface TriageInfo {
  // `null` means never classified; `'unknown'` means classified and the classifier could
  // not tell. Collapsing the two would hide whether triage has run at all.
  content_type: string | null
  confidence: number | null
  method: string | null
  model_name: string | null
  computed_at_ms: number | null
}

export interface SourceSummary {
  id: string
  platform: string
  external_id: string
  source_type: string
  title: string | null
  caption: string | null
  creator: Creator | null
  source_url: string | null
  cover_url: string | null
  published_at_ms: number | null
  saved_at_ms: number | null
  duration_ms: number | null
  availability: string
  processing: ProcessingState
  triage: TriageInfo
  collections?: { id: string; name: string | null }[]
}

export interface EvidenceUnit {
  id: string
  kind: string
  text: string | null
  start_ms: number | null
  end_ms: number | null
}

export interface ProcessingRunRow {
  id: string
  status: string
  target_level: number
  achieved_level: number | null
  is_current: boolean
  started_at_ms: number | null
  finished_at_ms: number | null
}

export interface SourceDetail extends SourceSummary {
  policy: PolicyStatus
  evidence: EvidenceUnit[]
  chunks: { id: string; text: string | null; chunk_type: string | null }[]
  // Null rather than [] when the source produced none, so components must not assume
  // an array. The API is the authority here; normalizing it away in the client would
  // hide the difference between "no claims" and "not processed".
  claims: ClaimRow[] | null
  mention_count: number
  runs: ProcessingRunRow[]
}

export interface ClaimRow {
  id: string
  predicate: string
  value_text: string | null
  value_number: number | null
  unit: string | null
  currency: string | null
  attribution: string
  confidence: number | null
  subject: string | null
}

export interface Collection {
  id: string
  platform: string
  external_collection_id: string
  name: string | null
  description: string | null
  source_count: number
  last_synced_at_ms: number | null
}

export interface Citation {
  ordinal: number
  marker: string
  source_id: string | null
  evidence_id: string | null
  claim_id: string | null
  wiki_page_id: string | null
  label: string | null
  source_title: string | null
  start_ms: number | null
  timestamp: string | null
  snippet: string | null
  precision: string | null
}

export interface TurnResult {
  conversation_id: string
  user_message_id: string
  assistant_message_id: string
  content: string
  scope: string
  resolved_query: string
  has_evidence: boolean
  citations: Citation[]
  conflicts: unknown[]
  suggestions: unknown[]
  meta: Record<string, unknown>
}

export interface SearchResult {
  chunk_id: string
  source_id: string
  text: string | null
  score: number
  chunk_type: string | null
  start_ms: number | null
  end_ms: number | null
  source_title: string | null
  evidence_ids: string[]
  retrieved_by: string[]
}

export interface WikiPageSummary {
  id: string
  page_type: string
  title: string
  slug: string
  entity_id: string | null
  topic_id: string | null
  status: string
  revision_no: number
  summary: string | null
  updated_at_ms: number | null
}

export interface WikiSupportRow {
  statement_key: string
  claim_id: string | null
  evidence_id: string | null
  /** The id the support row points at; null for fact-level statements. */
  source_id: string | null
  /** The source it resolves to after following the claim or evidence. Always set. */
  resolved_source_id: string | null
  source_title: string | null
  cited_as: 'claim' | 'evidence' | 'source'
  support_role: string
}

export interface WikiPageDetail extends WikiPageSummary {
  canonical_key: string
  revision: {
    id: string
    revision_no: number
    mutation_type: string
    content_markdown: string
    summary: string | null
    frontmatter: Record<string, unknown>
    content_hash: string
  }
  supports: WikiSupportRow[]
  links: unknown[]
  open_findings: unknown[]
}

export interface Stats {
  demo_mode: boolean
  capture_provider: string
  corpus: {
    collections: number
    creators: number
    sources: number
    locally_deleted: number
    processed_sources: number
    processing_by_status: Record<string, number>
  }
  knowledge: {
    evidence_units: number
    retrieval_chunks: number
    entities: number
    mentions: number
    ambiguous_mentions: number
    claims: number
    wiki_pages: number
    wiki_revisions: number
    open_lint_findings: number
  }
  index: {
    search_documents: number
    documents_by_type: Record<string, number>
    vector_documents: number
  }
  conversations: { conversations: number; messages: number }
  queue: {
    by_status: Record<string, number>
    runs_total: number
    runs_failed: number
  }
}

export interface PolicyRule {
  id: string
  name: string | null
  is_enabled: boolean
  rule_type: string
  action: string
  priority: number
  target_source_id: string | null
  target_creator_id: string | null
  target_collection_id: string | null
  matcher: Record<string, unknown> | null
  origin: string
  created_at_ms?: number
}

export interface PolicyDecisionRow {
  id: string
  source_id: string
  rule_id: string | null
  phase: string
  action: string
  reason_code: string | null
  explanation: Record<string, unknown> | null
  created_at_ms: number
}

export interface SettingsView {
  demo_mode: boolean
  settings: Record<string, unknown>
  resolved_models: Record<string, string>
}
