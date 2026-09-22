/**
 * Fetch wrapper over the local API.
 *
 * Requests are same-origin and relative: in development Vite proxies them, in production
 * FastAPI serves the bundle itself. No base URL is baked into the build, so the same
 * artifact works either way.
 *
 * The backend returns domain errors as `{error: {code, message, ...}}` with a real HTTP
 * status. Those codes are the useful part -- `not_found` and `validation_error` need
 * different treatment in the UI -- so they are preserved on the thrown error rather than
 * flattened into a string.
 */

import type {
  Collection,
  EntityDetail,
  EntitySummary,
  EntityUserState,
  PolicyDecisionRow,
  ResurfaceCard,
  PolicyRule,
  SearchResult,
  SettingsView,
  SourceDetail,
  SourceSummary,
  Stats,
  TurnResult,
  WikiPageDetail,
  WikiPageSummary,
} from './types'

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly detail: unknown

  constructor(status: number, code: string, message: string, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers: {
        ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
        ...init?.headers,
      },
    })
  } catch (cause) {
    // A local-first app's most common failure is that the backend simply is not running.
    // Saying so is more useful than "Failed to fetch", which sends people looking for a
    // network problem that does not exist.
    throw new ApiError(0, 'unreachable', '连接不上本地服务，确认 dk serve 正在运行', cause)
  }

  if (response.status === 204) return undefined as T

  const text = await response.text()
  const body = text ? (JSON.parse(text) as unknown) : null

  if (!response.ok) {
    const error = (body as { error?: { code?: string; message?: string } } | null)?.error
    throw new ApiError(
      response.status,
      error?.code ?? 'http_error',
      error?.message ?? `请求失败（${response.status}）`,
      body,
    )
  }
  return body as T
}

const qs = (params: Record<string, string | number | boolean | undefined>) => {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ''
}

export const api = {
  health: () => request<{ status: string; demo_mode: boolean }>('/health'),

  // ---------------------------------------------------------------- sources
  collections: () => request<{ collections: Collection[] }>('/api/sources/collections'),

  sources: (params: { collection_id?: string; status?: string; limit?: number; offset?: number } = {}) =>
    request<{ total: number; limit: number; offset: number; sources: SourceSummary[] }>(
      `/api/sources${qs(params)}`,
    ),

  source: (id: string) => request<SourceDetail>(`/api/sources/${encodeURIComponent(id)}`),

  sync: (body: { collection_id?: string; auto_process?: boolean } = {}) =>
    request<{ job_id: string; queued: boolean }>('/api/sources/sync', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  processSource: (id: string, body: { target_level?: number; force?: boolean } = {}) =>
    request<{ job_id: string; queued: boolean }>(
      `/api/sources/${encodeURIComponent(id)}/process`,
      { method: 'POST', body: JSON.stringify(body) },
    ),

  deleteSource: (id: string) =>
    request<unknown>(`/api/sources/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // ---------------------------------------------------------------- knowledge
  search: (q: string, limit = 20) =>
    request<{
      query: string
      results: SearchResult[]
      source_ids: string[]
      is_empty: boolean
      diagnostics: Record<string, unknown>
    }>(`/api/knowledge/search${qs({ q, limit })}`),

  wiki: (params: { q?: string; limit?: number } = {}) =>
    request<{ total: number; pages: WikiPageSummary[] }>(`/api/knowledge/wiki${qs(params)}`),

  wikiPage: (id: string) =>
    request<WikiPageDetail>(`/api/knowledge/wiki/${encodeURIComponent(id)}`),

  entities: (params: { q?: string; entity_type?: string; min_claims?: number; limit?: number } = {}) =>
    request<{ total: number; entities: EntitySummary[] }>(
      `/api/knowledge/entities${qs(params)}`,
    ),

  entity: (id: string) =>
    request<EntityDetail>(`/api/knowledge/entities/${encodeURIComponent(id)}`),

  // ---------------------------------------------------------------- resurface
  // The user's own saved intentions. Separate from claims on purpose: what the user wants
  // is not something a creator said.
  resurface: (params: { state?: string; limit?: number } = {}) =>
    request<{ total: number; states: string[]; cards: ResurfaceCard[] }>(
      `/api/knowledge/resurface${qs(params)}`,
    ),

  setUserState: (entityId: string, body: { state: string; note?: string | null }) =>
    request<EntityUserState>(
      `/api/knowledge/entities/${encodeURIComponent(entityId)}/user-state`,
      { method: 'PUT', body: JSON.stringify(body) },
    ),

  clearUserState: (entityId: string) =>
    request<EntityUserState & { cleared: boolean }>(
      `/api/knowledge/entities/${encodeURIComponent(entityId)}/user-state`,
      { method: 'DELETE' },
    ),

  // ---------------------------------------------------------------- ask
  // `scope_override`, not `scope`: the field name matters because the route now rejects
  // unknown keys instead of dropping them, which is how the earlier mismatch went unnoticed.
  ask: (body: {
    query: string
    conversation_id?: string
    scope_override?: string
    limit?: number
  }) =>
    request<TurnResult>('/api/conversations/ask', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // ---------------------------------------------------------------- admin
  stats: () => request<Stats>('/api/admin/stats'),
  settings: () => request<SettingsView>('/api/admin/settings'),
  reindex: () => request<{ job_id: string }>('/api/admin/reindex', { method: 'POST' }),

  rules: () => request<{ rules: PolicyRule[] }>('/api/admin/policy/rules'),

  createRule: (body: Partial<PolicyRule>) =>
    request<PolicyRule>('/api/admin/policy/rules', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  deleteRule: (id: string) =>
    request<unknown>(`/api/admin/policy/rules/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),

  decisions: (params: { source_id?: string; limit?: number } = {}) =>
    request<{ decisions: PolicyDecisionRow[] }>(`/api/admin/policy/decisions${qs(params)}`),
}
