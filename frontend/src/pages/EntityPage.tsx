import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client'
import { INTENT_STATES, entityTypeLabel, errorMessage } from '../lib/format'

/**
 * Set or clear the user's own intention for this entity.
 *
 * Deliberately placed above the claims and visually separate from them: the claims are what
 * other people said, this is what the user said, and a UI that stacks them in one list makes
 * the distinction the data model insists on (KM-003) invisible.
 */
function IntentionControl({ entityId, current, note }: {
  entityId: string
  current: string | null
  note: string | null
}) {
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState(note ?? '')

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['entity', entityId] })
    queryClient.invalidateQueries({ queryKey: ['resurface'] })
  }

  const save = useMutation({
    mutationFn: (state: string) => api.setUserState(entityId, { state, note: draft || null }),
    onSuccess: invalidate,
  })
  const clear = useMutation({
    mutationFn: () => api.clearUserState(entityId),
    onSuccess: invalidate,
  })

  return (
    <section>
      <h2>我的标记</h2>
      <p className="muted" style={{ fontSize: '0.85rem', maxWidth: '68ch' }}>
        这是你自己记下的，跟作者说了什么无关。标记过的会出现在「待办」里，
        就算之后把相关视频设成不处理，标记也不会丢。
      </p>
      <div className="chips">
        {INTENT_STATES.map((state) => (
          <button
            key={state.value}
            className={current === state.value ? 'btn' : 'btn btn--quiet'}
            onClick={() => save.mutate(state.value)}
            disabled={save.isPending}
          >
            {state.label}
          </button>
        ))}
        {current ? (
          <button
            className="btn btn--quiet"
            onClick={() => clear.mutate()}
            disabled={clear.isPending}
            title="取消标记，你写的备注会留着"
          >
            取消标记
          </button>
        ) : null}
      </div>
      <div className="field" style={{ marginTop: '0.75rem', maxWidth: '32rem' }}>
        <label htmlFor="intent-note">备注</label>
        <input
          id="intent-note"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={() => {
            if (current && draft !== (note ?? '')) save.mutate(current)
          }}
          placeholder="为什么记下这个"
        />
      </div>
      {save.isError ? <p className="notice notice--error">{errorMessage(save.error)}</p> : null}
    </section>
  )
}

export function EntityPage() {
  const { entityId = '' } = useParams()
  const entity = useQuery({
    queryKey: ['entity', entityId],
    queryFn: () => api.entity(entityId),
    enabled: Boolean(entityId),
  })

  if (entity.isPending) return <p className="muted">读取中…</p>
  if (entity.isError) return <p className="notice notice--error">{errorMessage(entity.error)}</p>

  const item = entity.data
  if (item.merged_into_entity_id) {
    // A merged entity is a redirect, not a page: the knowledge now lives under the survivor.
    return (
      <div className="empty">
        <h3>{item.canonical_name} 已合并</h3>
        <p className="muted">
          <Link to={`/entities/${item.merged_into_entity_id}`}>去看合并后的条目 →</Link>
        </p>
      </div>
    )
  }

  const claims = item.claims ?? []
  // Grouped by source, because a claim is never a free-floating fact: the honest rendering
  // is "this video said X", which requires the video to head the group.
  const bySource = new Map<string, typeof claims>()
  for (const claim of claims) {
    const bucket = bySource.get(claim.source_id) ?? []
    bucket.push(claim)
    bySource.set(claim.source_id, bucket)
  }

  return (
    <div className="stack">
      <div>
        <Link className="faint" to="/resurface">
          ← 回到待办
        </Link>
        <h1 style={{ marginTop: '0.6rem' }}>{item.canonical_name}</h1>
        <p className="faint">
          {entityTypeLabel(item.entity_type)}
          {item.aliases?.length ? ` · 也叫 ${item.aliases.join('、')}` : ''}
          {item.wiki_page_id ? (
            <>
              {' · '}
              <Link to={`/wiki/${item.wiki_page_id}`}>看条目</Link>
            </>
          ) : null}
        </p>
      </div>

      <IntentionControl
        entityId={item.id}
        current={item.user_state?.state ?? null}
        note={item.user_state?.note ?? null}
      />

      <section>
        <h2>收藏里怎么说</h2>
        {claims.length === 0 ? (
          <p className="notice">
            现在没有可引用的说法。可能相关视频还没处理，也可能被规则设成了不处理。
          </p>
        ) : (
          [...bySource.entries()].map(([sourceId, rows]) => (
            <div key={sourceId} style={{ marginBottom: '1.1rem' }}>
              <p style={{ margin: '0 0 0.35rem' }}>
                <Link to={`/sources/${sourceId}`}>{rows[0]?.source_title ?? sourceId}</Link>
              </p>
              <table>
                <tbody>
                  {rows.map((claim) => (
                    <tr key={claim.id}>
                      <td className="mono">{claim.predicate}</td>
                      <td>
                        {claim.value_text ??
                          [claim.value_number, claim.unit ?? claim.currency]
                            .filter((v) => v !== null && v !== undefined)
                            .join(' ')}
                      </td>
                      <td className="faint">{claim.attribution}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))
        )}
      </section>
    </div>
  )
}
