import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import type { ResurfaceCard } from '../api/types'
import { INTENT_STATES, dateTime, errorMessage, intentLabel } from '../lib/format'

/**
 * One saved intention, with whatever current knowledge still backs it.
 *
 * A card with no support is shown, not hidden. The intention is the user's own statement, so
 * removing it because a video was excluded would delete their data over a policy decision
 * about someone else's content; saying so plainly is the honest alternative.
 */
function Card({ card }: { card: ResurfaceCard }) {
  return (
    <article className="card">
      <h3 style={{ margin: '0 0 0.3rem' }}>
        <Link to={`/entities/${card.entity_id}`}>{card.canonical_name}</Link>
      </h3>
      <p className="faint" style={{ margin: '0 0 0.6rem', fontSize: '0.82rem' }}>
        {intentLabel(card.state)}
        {card.last_action_at_ms ? ` · ${dateTime(card.last_action_at_ms)}标记` : ''}
      </p>
      {card.note ? <p style={{ margin: '0 0 0.6rem' }}>{card.note}</p> : null}

      {card.has_eligible_support ? (
        <ul style={{ margin: 0, paddingLeft: '1.1rem' }}>
          {card.supports.map((support) => (
            <li key={support.claim_id} style={{ marginBottom: '0.25rem' }}>
              <span className="mono faint">{support.predicate}</span>{' '}
              {support.value_text ??
                [support.value_number, support.unit ?? support.currency]
                  .filter((v) => v !== null && v !== undefined)
                  .join(' ')}
              {' — '}
              <Link to={`/sources/${support.source_id}`}>
                {support.source_title ?? support.source_id}
              </Link>
            </li>
          ))}
        </ul>
      ) : (
        <p className="notice" style={{ margin: 0 }}>
          收藏里现在没有可引用的说法支持这条。可能相关视频被设成了不处理，也可能还没处理完。
          标记本身留着。
        </p>
      )}

      <p style={{ marginBottom: 0, marginTop: '0.6rem' }}>
        <Link className="faint" to={`/entities/${card.entity_id}`}>
          看全部出处 →
        </Link>
        {card.wiki_page_id ? (
          <>
            {' · '}
            <Link className="faint" to={`/wiki/${card.wiki_page_id}`}>
              看条目
            </Link>
          </>
        ) : null}
      </p>
    </article>
  )
}

export function ResurfacePage() {
  const [state, setState] = useState<string | null>(null)
  const cards = useQuery({
    queryKey: ['resurface', state],
    queryFn: () => api.resurface(state ? { state } : {}),
  })

  return (
    <div className="stack">
      <div>
        <h1>待办</h1>
        <p className="lede">
          你自己标记过想去、想试、想学的东西。排序固定按最近标记的在前，不做推荐、不打分。
        </p>
      </div>

      <div className="chips">
        <button
          className={state === null ? 'btn' : 'btn btn--quiet'}
          onClick={() => setState(null)}
        >
          全部
        </button>
        {INTENT_STATES.map((option) => (
          <button
            key={option.value}
            className={state === option.value ? 'btn' : 'btn btn--quiet'}
            onClick={() => setState(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>

      {cards.isPending ? <p className="muted">读取中…</p> : null}
      {cards.isError ? (
        <p className="notice notice--error">{errorMessage(cards.error)}</p>
      ) : null}

      {cards.data && cards.data.cards.length === 0 ? (
        <div className="empty">
          <h3>还没有标记</h3>
          <p className="muted">
            在实体页上点「想去 / 想试 / 想学」，标记过的会出现在这里。
          </p>
        </div>
      ) : null}

      <div className="cards">
        {cards.data?.cards.map((card) => (
          <Card key={card.entity_id} card={card} />
        ))}
      </div>
    </div>
  )
}
