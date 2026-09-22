/** Formatting helpers. Chinese locale throughout: this is a Chinese-language interface. */

export function timecode(ms: number | null | undefined): string {
  if (ms == null) return '—'
  const total = Math.floor(ms / 1000)
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}:${String(s).padStart(2, '0')}`
}

export function duration(ms: number | null | undefined): string {
  if (ms == null) return '—'
  const total = Math.round(ms / 1000)
  if (total < 60) return `${total} 秒`
  const m = Math.floor(total / 60)
  const s = total % 60
  return s ? `${m} 分 ${s} 秒` : `${m} 分`
}

export function date(ms: number | null | undefined): string {
  if (ms == null) return '—'
  return new Date(ms).toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  })
}

export function dateTime(ms: number | null | undefined): string {
  if (ms == null) return '—'
  return new Date(ms).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Processing status, in the user's terms rather than the column's. */
export const PROCESSING_LABELS: Record<string, string> = {
  pending: '待处理',
  queued: '排队中',
  running: '处理中',
  processed: '已处理',
  partial: '部分处理',
  failed: '处理失败',
  skipped: '已跳过',
  excluded: '按规则排除',
}

export const statusLabel = (status: string) => PROCESSING_LABELS[status] ?? status

/**
 * Cheap triage labels (DEC-016).
 *
 * `unknown` is worded as an admission rather than a category: it means triage ran and
 * could not tell, which is why such a source is still processed instead of skipped.
 */
export const CONTENT_TYPE_LABELS: Record<string, string> = {
  movie_clip: '影视剪辑',
  variety_clip: '综艺片段',
  music_clip: '音乐/MV',
  meme: '搞笑段子',
  sports_highlight: '体育集锦',
  other_entertainment: '其他娱乐',
  knowledge: '知识内容',
  unknown: '暂未判定',
}

export const contentTypeLabel = (value: string | null | undefined) =>
  value ? (CONTENT_TYPE_LABELS[value] ?? value) : '未分类'

/**
 * Knowledge scope, explained rather than named.
 *
 * `personal_first` is the default for an unmarked question (RET-003), so its wording has
 * to make the behaviour obvious without the user knowing the term.
 */
export const SCOPE_LABELS: Record<string, string> = {
  personal_required: '只用你的收藏回答',
  personal_first: '先看你的收藏',
  general: '通用知识',
  hybrid: '收藏加通用知识',
}

export const scopeLabel = (scope: string) => SCOPE_LABELS[scope] ?? scope

/** The saved-intention states, in the order they are offered.
 *
 * Mirrors `knowledge.resurface.INTENT_STATES`; the API rejects anything else, so adding a
 * button here without adding the state there produces a 422 rather than a silent no-op.
 */
export const INTENT_STATES: Array<{ value: string; label: string }> = [
  { value: 'want_to_go', label: '想去' },
  { value: 'want_to_try', label: '想试' },
  { value: 'want_to_learn', label: '想学' },
]

export const intentLabel = (state: string) =>
  INTENT_STATES.find((s) => s.value === state)?.label ?? state

export const ENTITY_TYPE_LABELS: Record<string, string> = {
  place: '地点',
  person: '人物',
  product: '商品',
  work: '作品',
  organization: '机构',
  concept: '概念',
  event: '事件',
  dish: '菜品',
}

export const entityTypeLabel = (kind: string) => ENTITY_TYPE_LABELS[kind] ?? kind

export const ACTION_LABELS: Record<string, string> = {
  process: '正常处理',
  metadata_only: '只存元数据',
  always_process: '总是深度处理',
  exclude: '不处理',
}

export const actionLabel = (action: string) => ACTION_LABELS[action] ?? action

export const RULE_TYPE_LABELS: Record<string, string> = {
  source: '单个视频',
  creator: '作者',
  collection: '收藏夹',
  metadata: '元数据条件',
  semantic: '语义判断',
}

export const ruleTypeLabel = (kind: string) => RULE_TYPE_LABELS[kind] ?? kind

export function errorMessage(error: unknown): string {
  if (error && typeof error === 'object' && 'message' in error) {
    return String((error as { message: unknown }).message)
  }
  return '出错了'
}
