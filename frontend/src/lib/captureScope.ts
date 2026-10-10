import type { CaptureScope, JobAccepted, NamedCollectionTarget } from '../api/types'

export function copyScope(scope: CaptureScope): CaptureScope {
  return { ...scope, named_collection_ids: [...scope.named_collection_ids] }
}

export function scopeChanged(saved: CaptureScope, draft: CaptureScope): boolean {
  if (saved.default_favorites !== draft.default_favorites) return true
  const left = [...new Set(saved.named_collection_ids)].sort()
  const right = [...new Set(draft.named_collection_ids)].sort()
  return JSON.stringify(left) !== JSON.stringify(right)
}

export function toggleNamed(scope: CaptureScope, id: string): CaptureScope {
  const selected = scope.named_collection_ids.includes(id)
  return {
    ...scope,
    named_collection_ids: selected
      ? scope.named_collection_ids.filter((value) => value !== id)
      : [...scope.named_collection_ids, id],
  }
}

/** Keep saved IDs visible even when discovery is unavailable or the request fails. */
export function visibleNamedTargets(
  saved: CaptureScope,
  discovered: NamedCollectionTarget[],
): NamedCollectionTarget[] {
  const rows = new Map(discovered.map((target) => [target.external_collection_id, target]))
  for (const id of saved.named_collection_ids) {
    if (!rows.has(id)) {
      rows.set(id, {
        external_collection_id: id,
        name: null,
        item_count: null,
        discovered: false,
        locally_observed: false,
        selected: true,
        last_synced_at_ms: null,
      })
    }
  }
  return [...rows.values()].sort((a, b) =>
    a.external_collection_id.localeCompare(b.external_collection_id),
  )
}

export function canSyncScope(saved: CaptureScope, draft: CaptureScope): boolean {
  return !scopeChanged(saved, draft) &&
    (saved.default_favorites || saved.named_collection_ids.length > 0)
}

export function syncAcceptanceMessage(job: JobAccepted): string {
  return job.created
    ? '新同步任务已受理；接受不代表同步完成。'
    : '复用了已有同步任务；本次 AI 处理勾选不会更改该任务原有的处理选项。复用不代表同步完成。'
}
