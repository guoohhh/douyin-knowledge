import { useMemo, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import type { Citation, TurnResult } from '../api/types'
import { errorMessage, scopeLabel, timecode } from '../lib/format'

const SCOPES = [
  { value: '', label: '自动判断' },
  { value: 'personal_required', label: '只用收藏' },
  { value: 'personal_first', label: '收藏优先' },
  { value: 'hybrid', label: '收藏 + 通用' },
  { value: 'general', label: '通用知识' },
]

/**
 * Render the answer, turning `[1]` markers in the text into real controls.
 *
 * The model writes markers inline, so they arrive as plain text. Leaving them as text would
 * make the citation decorative; making them focusable buttons is what lets a reader move
 * from a sentence to the evidence behind it, which is the one interaction this product
 * exists to support.
 */
function AnswerText({
  content,
  citations,
  active,
  onFocusCitation,
}: {
  content: string
  citations: Citation[]
  active: number | null
  onFocusCitation: (ordinal: number | null) => void
}) {
  const known = useMemo(() => new Set(citations.map((c) => c.ordinal)), [citations])

  const parts = useMemo(() => content.split(/(\[\d+\])/g), [content])

  return (
    <div className="answer">
      {parts.map((part, index) => {
        const match = /^\[(\d+)\]$/.exec(part)
        const ordinal = match?.[1] ? Number(match[1]) : null
        // A marker with no matching citation is left as plain text on purpose: rendering it
        // as a control that leads nowhere would promise provenance that does not exist.
        if (ordinal === null || !known.has(ordinal)) {
          return <span key={index}>{part}</span>
        }
        return (
          <button
            key={index}
            type="button"
            className="marker"
            data-active={active === ordinal}
            aria-label={`查看第 ${ordinal} 条依据`}
            onClick={() => onFocusCitation(active === ordinal ? null : ordinal)}
          >
            [{ordinal}]
          </button>
        )
      })}
    </div>
  )
}

function EvidenceRail({
  citations,
  active,
  onFocusCitation,
}: {
  citations: Citation[]
  active: number | null
  onFocusCitation: (ordinal: number | null) => void
}) {
  return (
    <aside className="rail" aria-label="依据">
      <h2>依据 · {citations.length} 条</h2>
      {citations.map((citation) => (
        <div
          key={`${citation.ordinal}-${citation.evidence_id ?? citation.claim_id}`}
          className="cite"
          data-active={active === citation.ordinal}
          onMouseEnter={() => onFocusCitation(citation.ordinal)}
          onMouseLeave={() => onFocusCitation(null)}
        >
          <div className="cite__head">
            <span>[{citation.ordinal}]</span>
            {citation.timestamp ? <span>{citation.timestamp}</span> : null}
            <span className="cite__title">{citation.source_title ?? '未知来源'}</span>
          </div>
          <p className="cite__snippet">{citation.snippet ?? citation.label ?? '（无摘录）'}</p>
          <div className="cite__foot">
            {citation.source_id ? (
              <Link to={`/sources/${citation.source_id}`}>
                打开这条收藏
                {citation.start_ms != null ? ` · ${timecode(citation.start_ms)}` : ''}
              </Link>
            ) : (
              '来源已不可用'
            )}
          </div>
        </div>
      ))}
    </aside>
  )
}

export function AskPage() {
  const [draft, setDraft] = useState('')
  const [scope, setScope] = useState('')
  const [turn, setTurn] = useState<TurnResult | null>(null)
  const [active, setActive] = useState<number | null>(null)

  const ask = useMutation({
    mutationFn: (query: string) =>
      api.ask({
        query,
        ...(scope ? { scope_override: scope } : {}),
        // Keeping the conversation id makes follow-ups like "第二家呢" resolvable; the
        // backend rewrites the query from the previous turn's entities.
        ...(turn ? { conversation_id: turn.conversation_id } : {}),
      }),
    onSuccess: (result) => {
      setTurn(result)
      setActive(null)
    },
  })

  const submit = (event: React.FormEvent) => {
    event.preventDefault()
    const query = draft.trim()
    if (query) ask.mutate(query)
  }

  return (
    <div className="stack">
      <form className="askbar" onSubmit={submit}>
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="问一句，比如：我收藏过哪家茶餐厅"
          aria-label="你的问题"
          autoFocus
        />
        <select
          value={scope}
          onChange={(event) => setScope(event.target.value)}
          aria-label="回答范围"
        >
          {SCOPES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <button className="btn" type="submit" disabled={ask.isPending || !draft.trim()}>
          {ask.isPending ? '检索中' : '提问'}
        </button>
      </form>

      {ask.isError ? (
        <p className="notice notice--error">{errorMessage(ask.error)}</p>
      ) : null}

      {!turn && !ask.isPending ? (
        <div className="empty">
          <h3>先问一个你隐约记得存过的东西</h3>
          <p className="muted">
            回答只会用你自己的收藏，每句话旁边都会标出它来自哪条视频的哪一段。
            收藏还是空的话，去<Link to="/settings">设置</Link>里同步一次。
          </p>
        </div>
      ) : null}

      {turn ? (
        <div className="ask">
          <div>
            <AnswerText
              content={turn.content}
              citations={turn.citations}
              active={active}
              onFocusCitation={setActive}
            />
            <div className="answer__meta">
              <span>{scopeLabel(turn.scope)}</span>
              {turn.resolved_query !== draft.trim() ? (
                <span className="faint">检索用的是「{turn.resolved_query}」</span>
              ) : null}
              {!turn.has_evidence ? (
                <span className="faint">收藏里没有能支持这个问题的内容</span>
              ) : null}
            </div>
          </div>

          {turn.citations.length > 0 ? (
            <EvidenceRail
              citations={turn.citations}
              active={active}
              onFocusCitation={setActive}
            />
          ) : (
            <aside className="rail" aria-label="依据">
              <h2>依据</h2>
              <p className="muted">
                这个回答没有引用你的收藏，所以不要把它当成你存过的东西。
              </p>
            </aside>
          )}
        </div>
      ) : null}
    </div>
  )
}
