import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { date, duration, errorMessage, statusLabel } from '../lib/format'

export function CollectionsPage() {
  const [collectionId, setCollectionId] = useState<string>('')

  const collections = useQuery({ queryKey: ['collections'], queryFn: api.collections })

  const sources = useQuery({
    queryKey: ['sources', collectionId],
    queryFn: () => api.sources({ ...(collectionId ? { collection_id: collectionId } : {}), limit: 100 }),
  })

  const rows = sources.data?.sources ?? []

  return (
    <div className="stack">
      <div>
        <h1>收藏</h1>
        <p className="lede">
          抖音收藏夹同步过来的原始条目。处理状态说明哪些已经能被检索到。
        </p>
      </div>

      <div className="chips">
        <button
          className="chip"
          aria-pressed={collectionId === ''}
          onClick={() => setCollectionId('')}
        >
          全部
        </button>
        {(collections.data?.collections ?? []).map((collection) => (
          <button
            key={collection.id}
            className="chip"
            aria-pressed={collectionId === collection.id}
            onClick={() => setCollectionId(collection.id)}
          >
            {collection.name ?? collection.external_collection_id}（{collection.source_count}）
          </button>
        ))}
        <Link className="btn btn--quiet" to="/settings#capture-scope-title">
          前往采集范围设置与同步
        </Link>
      </div>

      {sources.isPending ? <p className="muted">读取中…</p> : null}
      {sources.isError ? (
        <p className="notice notice--error">{errorMessage(sources.error)}</p>
      ) : null}

      {sources.isSuccess && rows.length === 0 ? (
        <div className="empty">
          <h3>这里还没有收藏</h3>
          <p className="muted">
            前往采集范围设置选择目标并手动同步。演示模式使用内置示例收藏，不需要登录。
          </p>
        </div>
      ) : null}

      {rows.length > 0 ? (
        <div className="rows">
          {rows.map((source) => (
            <Link className="row" key={source.id} to={`/sources/${source.id}`}>
              <div>
                <div className="row__title">{source.title ?? source.caption ?? source.external_id}</div>
                <div className="row__sub">
                  {source.creator?.display_name ?? '未知作者'}
                  {source.duration_ms != null ? `　${duration(source.duration_ms)}` : ''}
                  {source.saved_at_ms != null ? `　存于 ${date(source.saved_at_ms)}` : ''}
                </div>
              </div>
              <div className="row__right">
                {statusLabel(source.processing.status)}
                <br />
                L{source.processing.achieved_level}
              </div>
            </Link>
          ))}
        </div>
      ) : null}

      {sources.data ? (
        <p className="faint mono">
          {rows.length} / {sources.data.total}
        </p>
      ) : null}
    </div>
  )
}
