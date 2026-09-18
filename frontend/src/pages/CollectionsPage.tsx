import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { date, duration, errorMessage, statusLabel } from '../lib/format'

export function CollectionsPage() {
  const queryClient = useQueryClient()
  const [collectionId, setCollectionId] = useState<string>('')

  const collections = useQuery({ queryKey: ['collections'], queryFn: api.collections })

  const sources = useQuery({
    queryKey: ['sources', collectionId],
    queryFn: () => api.sources({ ...(collectionId ? { collection_id: collectionId } : {}), limit: 100 }),
  })

  const sync = useMutation({
    mutationFn: () => api.sync({ auto_process: true }),
    onSuccess: () => {
      // The sync runs in the worker, so the counts move some seconds later. Invalidating
      // now shows the queued state; the 5s staleTime picks up the rest without a refresh.
      queryClient.invalidateQueries({ queryKey: ['collections'] })
      queryClient.invalidateQueries({ queryKey: ['sources'] })
      queryClient.invalidateQueries({ queryKey: ['stats'] })
    },
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
        <button
          className="btn btn--quiet"
          onClick={() => sync.mutate()}
          disabled={sync.isPending}
        >
          {sync.isPending ? '同步已排队' : '同步收藏夹'}
        </button>
      </div>

      {sync.isError ? <p className="notice notice--error">{errorMessage(sync.error)}</p> : null}
      {sync.isSuccess ? (
        <p className="notice">同步已交给后台，处理完这里的状态会自己更新。</p>
      ) : null}

      {sources.isPending ? <p className="muted">读取中…</p> : null}
      {sources.isError ? (
        <p className="notice notice--error">{errorMessage(sources.error)}</p>
      ) : null}

      {sources.isSuccess && rows.length === 0 ? (
        <div className="empty">
          <h3>这里还没有收藏</h3>
          <p className="muted">
            点上面的「同步收藏夹」。演示模式下会拉取一份内置的示例收藏，不需要登录。
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
