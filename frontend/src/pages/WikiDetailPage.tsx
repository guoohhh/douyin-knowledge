import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'

import { api } from '../api/client'
import { errorMessage } from '../lib/format'

const CITED_AS_LABELS: Record<string, string> = {
  claim: '结论',
  evidence: '原文片段',
  source: '整条收藏',
}

export function WikiDetailPage() {
  const { pageId = '' } = useParams()

  const page = useQuery({
    queryKey: ['wikiPage', pageId],
    queryFn: () => api.wikiPage(pageId),
    enabled: Boolean(pageId),
  })

  if (page.isPending) return <p className="muted">读取中…</p>
  if (page.isError) return <p className="notice notice--error">{errorMessage(page.error)}</p>

  const item = page.data
  const supports = item.supports ?? []

  return (
    <div className="stack">
      <div>
        <Link className="faint" to="/wiki">
          ← 回到条目
        </Link>
        <h1 style={{ marginTop: '0.6rem' }}>{item.title}</h1>
        <p className="faint mono">
          第 {item.revision.revision_no} 版 · {supports.length} 条出处
        </p>
      </div>

      {/* The markdown is rendered as preformatted text rather than parsed to HTML. A wiki
          page is compiled from the user's own collection, but it still passes through a
          model, and injecting model output into the DOM as markup is not a risk worth
          taking for slightly nicer headings. */}
      <div className="wikibody">{item.revision.content_markdown}</div>

      <section>
        <h2>每句话的出处</h2>
        {supports.length === 0 ? (
          // An uncited page is a defect, not a display case: the whole point of a compiled
          // page is that every statement can be walked back to a video.
          <p className="notice notice--error">
            这一版没有记录出处，属于编译缺陷。别把上面的内容当成已经核对过的知识。
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>陈述</th>
                <th>来自</th>
                <th>引用的是</th>
                <th>作用</th>
              </tr>
            </thead>
            <tbody>
              {supports.map((support, index) => (
                <tr key={`${support.statement_key}-${index}`}>
                  <td className="mono">{support.statement_key}</td>
                  <td>
                    {support.resolved_source_id ? (
                      <Link to={`/sources/${support.resolved_source_id}`}>
                        {support.source_title ?? support.resolved_source_id}
                      </Link>
                    ) : (
                      '来源已不可用'
                    )}
                  </td>
                  <td>{CITED_AS_LABELS[support.cited_as] ?? support.cited_as}</td>
                  <td>{support.support_role === 'supports' ? '支持' : support.support_role}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}
