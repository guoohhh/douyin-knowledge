import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { dateTime, errorMessage } from '../lib/format'

export function WikiPage() {
  const [query, setQuery] = useState('')

  const pages = useQuery({
    queryKey: ['wiki', query],
    queryFn: () => api.wiki({ ...(query ? { q: query } : {}), limit: 100 }),
  })

  const rows = pages.data?.pages ?? []

  return (
    <div className="stack">
      <div>
        <h1>条目</h1>
        <p className="lede">
          同一个地方、工具或话题在多条收藏里出现过，就会被合成一个条目。内容是编译出来的，
          随时可以从收藏重建。
        </p>
      </div>

      <div className="askbar">
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="按名字找条目"
          aria-label="搜索条目"
        />
      </div>

      {pages.isError ? <p className="notice notice--error">{errorMessage(pages.error)}</p> : null}

      {pages.isSuccess && rows.length === 0 ? (
        <div className="empty">
          <h3>{query ? '没有匹配的条目' : '还没有条目'}</h3>
          <p className="muted">
            {query
              ? '换个说法试试，条目名来自视频里实际提到的名字。'
              : '条目在收藏被处理后自动编译。先去收藏页同步并处理几条。'}
          </p>
        </div>
      ) : null}

      {rows.length > 0 ? (
        <div className="rows">
          {rows.map((page) => (
            <Link className="row" key={page.id} to={`/wiki/${page.id}`}>
              <div>
                <div className="row__title">{page.title}</div>
                <div className="row__sub">{page.summary ?? '（还没有摘要）'}</div>
              </div>
              <div className="row__right">
                第 {page.revision_no} 版
                <br />
                {dateTime(page.updated_at_ms)}
              </div>
            </Link>
          ))}
        </div>
      ) : null}
    </div>
  )
}
