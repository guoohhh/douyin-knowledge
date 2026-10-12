import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type { DailySyncSettings } from '../api/types'
import { dateTime, errorMessage } from '../lib/format'

const outcomeLabel: Record<string, string> = {
  queued: '已排队，尚未完成',
  running: '正在同步',
  succeeded: '所有目标同步完成',
  completed_with_skips: '任务已结束；部分目标在执行前被取消选择，未进行上游读取',
  failed: '同步失败，可用“立即同步”手动重试',
  no_targets: '已执行，但当时没有选中的目标',
  skipped_empty_scope: '跳过：采集范围为空',
}

const scheduledTime = (value: number, timezone: string) => new Intl.DateTimeFormat('zh-CN', {
  timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hour12: false,
}).format(new Date(value))

export function DailySyncSection() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['daily-sync'], queryFn: api.dailySync, refetchInterval: 30_000,
  })
  const [draft, setDraft] = useState<Pick<DailySyncSettings, 'enabled' | 'local_time' | 'timezone'> | null>(null)
  useEffect(() => {
    if (query.data) setDraft((current) => current ?? {
      enabled: query.data.enabled,
      local_time: query.data.local_time.slice(0, 5),
      timezone: query.data.timezone,
    })
  }, [query.data])
  const save = useMutation({
    mutationFn: api.saveDailySync,
    onSuccess: (saved) => {
      queryClient.setQueryData(['daily-sync'], saved)
      setDraft({ enabled: saved.enabled, local_time: saved.local_time.slice(0, 5), timezone: saved.timezone })
    },
  })
  const saved = query.data
  const dirty = !!draft && !!saved && (
    draft.enabled !== saved.enabled || draft.local_time !== saved.local_time.slice(0, 5) ||
    draft.timezone !== saved.timezone
  )

  return (
    <section aria-labelledby="daily-sync-title">
      <h2 id="daily-sync-title">每日自动同步</h2>
      <p className="muted">
        默认关闭。启用后，后台 worker 按保存的采集范围同步元数据，不会自动启动付费 AI 处理，也不会更改处理策略。
        手动“立即同步”仍在上方的采集范围区域。
      </p>
      <p className="faint">
        本地应用和 worker 运行时才会按时提交；电脑休眠或关闭期间不会运行。恢复或重启后会补交一次逾期同步，不会逐日补交。
        关闭日程会停止今后的自动提交，不会取消已经排队或运行的任务。
      </p>
      {query.isError ? <p className="notice notice--error">读取日程失败：{errorMessage(query.error)}</p> : null}
      {draft && saved ? (
        <>
          <label>
            <input type="checkbox" checked={draft.enabled}
              onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} />
            启用每日自动同步
          </label>
          <div className="capture-scope__actions">
            <label>本地时间 <input type="time" value={draft.local_time}
              onChange={(event) => setDraft({ ...draft, local_time: event.target.value })} /></label>
            <label>时区 <input type="text" value={draft.timezone} aria-label="IANA 时区"
              onChange={(event) => setDraft({ ...draft, timezone: event.target.value })} /></label>
            <button className="btn" type="button" disabled={!dirty || save.isPending}
              onClick={() => save.mutate(draft)}>{save.isPending ? '正在保存…' : '保存日程'}</button>
          </div>
          <p className="faint">时区使用 IANA 名称，例如 Asia/Shanghai；每日时间为该时区的当地时间。</p>
          <p>状态：{saved.enabled ? '已启用' : '已关闭'} · 下次预计：{saved.next_run_at_ms == null ? '未安排' : `${scheduledTime(saved.next_run_at_ms, saved.timezone)} (${saved.timezone})`}</p>
          <p>上次调度尝试：{saved.last_attempt_at_ms == null ? '无' : dateTime(saved.last_attempt_at_ms)}</p>
          <p>上次已知结果：{saved.last_outcome == null ? '无' : outcomeLabel[saved.last_outcome] ?? saved.last_outcome}</p>
          {saved.last_job_id ? <p className="faint">任务：<span className="mono">{saved.last_job_id}</span></p> : null}
          {save.isSuccess ? <p className="notice">日程已保存。</p> : null}
          {save.isError ? <p className="notice notice--error">保存失败：{errorMessage(save.error)}</p> : null}
        </>
      ) : query.isPending ? <p className="muted">正在读取日程…</p> : null}
    </section>
  )
}
