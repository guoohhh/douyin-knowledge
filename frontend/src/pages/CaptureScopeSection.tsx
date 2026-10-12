import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type { CaptureScope } from '../api/types'
import { dateTime, errorMessage, statusLabel } from '../lib/format'
import { canSyncScope, copyScope, scopeChanged, syncAcceptanceMessage, toggleNamed, visibleNamedTargets } from '../lib/captureScope'

const lastSync = (value: number | null) =>
  value == null ? '从未完整同步' : `上次完整同步：${dateTime(value)}`

export function CaptureScopeSection() {
  const queryClient = useQueryClient()
  const savedQuery = useQuery({ queryKey: ['capture-scope'], queryFn: api.captureScope })
  // First-read fixture bootstrap writes one setting. Sequence these reads so a fresh
  // database never races two initializers against the same unique app_settings key.
  const targetsQuery = useQuery({
    queryKey: ['capture-targets'],
    queryFn: api.captureTargets,
    enabled: savedQuery.isSuccess,
  })
  const [draft, setDraft] = useState<CaptureScope | null>(null)
  const [autoProcess, setAutoProcess] = useState(false)

  const saved = savedQuery.data ?? null
  useEffect(() => {
    if (saved) setDraft((current) => current ?? copyScope(saved))
  }, [saved])

  const save = useMutation({
    mutationFn: (scope: CaptureScope) => api.saveCaptureScope(scope),
    onSuccess: (persisted) => {
      queryClient.setQueryData(['capture-scope'], persisted)
      setDraft(copyScope(persisted))
      queryClient.invalidateQueries({ queryKey: ['capture-targets'] })
    },
  })
  const sync = useMutation({
    mutationFn: () => api.sync({ auto_process: autoProcess }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['stats'] }),
  })

  const dirty = saved != null && draft != null && scopeChanged(saved, draft)
  const rows = saved ? visibleNamedTargets(saved, targetsQuery.data?.named_collections ?? []) : []
  const discoveryFailed = targetsQuery.data?.discovery.state === 'error'
  const canSync = saved != null && draft != null && canSyncScope(saved, draft) &&
    !save.isPending && !sync.isPending
  const defaultTarget = targetsQuery.data?.default_favorites

  return (
    <section className="capture-scope" aria-labelledby="capture-scope-title">
      <h2 id="capture-scope-title">采集范围</h2>
      <p className="muted">
        决定哪些抖音收藏会进入本地知识库。取消选择只会停止今后观察，不会删除已保存的来源或知识。
        下方的处理策略另行决定进入后的内容是否使用 AI 处理。
      </p>

      {savedQuery.isPending ? <p className="muted">正在读取已保存的采集范围…</p> : null}
      {savedQuery.isError ? (
        <p className="notice notice--error">读取已保存范围失败：{errorMessage(savedQuery.error)}</p>
      ) : null}

      {saved && draft ? (
        <>
          <div className="capture-scope__heading">
            <h3>可选目标</h3>
            <button
              className="btn btn--quiet"
              type="button"
              onClick={() => targetsQuery.refetch()}
              disabled={targetsQuery.isFetching}
            >
              {targetsQuery.isFetching ? '正在发现…' : '重新发现'}
            </button>
          </div>

          {discoveryFailed ? (
            <p className="notice notice--error" role="status">
              收藏夹发现失败（{targetsQuery.data?.discovery.error_code ?? '未知错误'}）。
              下面保留已保存和本地记录；当前无法确认上游是否还有其他收藏夹。你仍可编辑并保存已有目标。
            </p>
          ) : targetsQuery.isError ? (
            <p className="notice notice--error" role="status">
              无法读取收藏夹发现结果：{errorMessage(targetsQuery.error)}。
              已保存的目标仍可编辑；不要把这次失败理解为上游收藏夹为空。
            </p>
          ) : null}

          <div className="capture-targets">
            <label className="capture-target">
              <input
                type="checkbox"
                checked={draft.default_favorites}
                disabled={save.isPending}
                onChange={(event) => setDraft({ ...draft, default_favorites: event.target.checked })}
              />
              <span>
                <strong>默认收藏 · 收藏 → 视频</strong>
                <span className="capture-target__meta">
                  {defaultTarget == null
                    ? '正在读取提供者能力'
                    : defaultTarget.available
                      ? '提供者支持此目标；不代表当前已登录或可访问'
                      : '当前提供者不支持此目标'}
                  {' · '}{lastSync(defaultTarget?.last_completed_at_ms ?? null)}
                  {saved.default_favorites ? ' · 已保存选中' : ''}
                </span>
              </span>
            </label>

            {rows.map((target) => (
              <label className="capture-target" key={target.external_collection_id}>
                <input
                  type="checkbox"
                  checked={draft.named_collection_ids.includes(target.external_collection_id)}
                  disabled={save.isPending}
                  onChange={() => setDraft(toggleNamed(draft, target.external_collection_id))}
                />
                <span>
                  <strong>{target.name ?? target.external_collection_id}</strong>
                  <span className="capture-target__meta">
                    <span className="mono">{target.external_collection_id}</span>
                    {' · '}{target.discovered
                      ? '本次已发现'
                      : discoveryFailed || targetsQuery.isError
                        ? '上游状态未确认'
                        : targetsQuery.isPending
                          ? '正在发现'
                          : '本次未发现'}
                    {' · '}{target.locally_observed ? '本地有历史记录' : '本地尚无记录'}
                    {target.item_count != null && target.discovered ? ` · 上游报告 ${target.item_count} 条` : ''}
                    {' · '}{lastSync(target.last_synced_at_ms)}
                    {saved.named_collection_ids.includes(target.external_collection_id) ? ' · 已保存选中' : ''}
                  </span>
                </span>
              </label>
            ))}
          </div>

          {rows.length === 0 ? (
            <p className="muted">
              {targetsQuery.isPending
                ? '正在读取收藏夹列表…'
                : discoveryFailed || targetsQuery.isError
                  ? '目前只能确认默认收藏目标；无法判断上游是否有命名收藏夹。'
                  : '本次没有发现命名收藏夹。你仍可选择默认收藏。'}
            </p>
          ) : null}
          {!saved.default_favorites && saved.named_collection_ids.length === 0 ? (
            <p className="faint">当前保存的是空范围；同步不会导入收藏内容。</p>
          ) : null}

          <label className="capture-scope__process">
            <input
              type="checkbox"
              checked={autoProcess}
              onChange={(event) => setAutoProcess(event.target.checked)}
            />
            为本次新建的同步任务启用处理策略（可能调用付费 AI 服务）
          </label>
          <p className="faint">
            未勾选时，本次请求创建的任务使用仅同步元数据选项；勾选后，新建的处理任务仍先经过下方的处理策略判断。
            已排队或运行的任务会沿用原有处理选项，可能触发 AI 处理；切换勾选不会修改或取消它们。
          </p>

          <div className="capture-scope__actions">
            <button
              className="btn"
              type="button"
              disabled={!dirty || save.isPending}
              onClick={() => { if (draft) save.mutate(draft) }}
            >
              {save.isPending ? '正在保存…' : '保存采集范围'}
            </button>
            <button
              className="btn btn--quiet"
              type="button"
              disabled={!canSync}
              onClick={() => { if (canSync) sync.mutate() }}
            >
              {sync.isPending ? '正在提交…' : '立即同步已保存范围'}
            </button>
          </div>

          {dirty ? <p className="notice">选择尚未保存。请先保存，再同步新范围。</p> : null}
          {save.isSuccess && !dirty ? <p className="notice">采集范围已保存，尚未开始同步。</p> : null}
          {save.isError ? <p className="notice notice--error">保存失败：{errorMessage(save.error)}</p> : null}

          {sync.isSuccess ? (
            <p className="notice" role="status">
              {syncAcceptanceMessage(sync.data)}
              （{statusLabel(sync.data.status)}，任务 <span className="mono">{sync.data.job_id}</span>）。
              请稍后查看任务、来源和处理状态。
            </p>
          ) : null}
          {sync.isError ? <p className="notice notice--error">同步提交失败：{errorMessage(sync.error)}</p> : null}
        </>
      ) : null}
    </section>
  )
}
