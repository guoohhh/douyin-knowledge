import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { api } from '../api/client'
import { actionLabel, date, dateTime, duration, errorMessage, statusLabel, timecode } from '../lib/format'

export function SourcePage() {
  const { sourceId = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const source = useQuery({
    queryKey: ['source', sourceId],
    queryFn: () => api.source(sourceId),
    enabled: Boolean(sourceId),
  })

  // The decision record is the answer to "why isn't this searchable?", so it belongs on the
  // page where the user is asking that question rather than buried in settings.
  const decisions = useQuery({
    queryKey: ['decisions', sourceId],
    queryFn: () => api.decisions({ source_id: sourceId, limit: 3 }),
    enabled: Boolean(sourceId),
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['source', sourceId] })
    queryClient.invalidateQueries({ queryKey: ['decisions', sourceId] })
  }

  const process = useMutation({
    mutationFn: (force: boolean) => api.processSource(sourceId, { force }),
    onSuccess: invalidate,
  })

  const remove = useMutation({
    mutationFn: () => api.deleteSource(sourceId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sources'] })
      navigate('/collections')
    },
  })

  if (source.isPending) return <p className="muted">读取中…</p>
  if (source.isError) return <p className="notice notice--error">{errorMessage(source.error)}</p>

  const item = source.data
  const claims = item.claims ?? []
  const latestDecision = decisions.data?.decisions?.[0]

  return (
    <div className="stack">
      <div>
        <Link className="faint" to="/collections">
          ← 回到收藏
        </Link>
        <h1 style={{ marginTop: '0.6rem' }}>{item.title ?? item.external_id}</h1>
        <p className="lede">{item.caption ?? '（没有文案）'}</p>
        <p className="muted" style={{ fontSize: '0.85rem' }}>
          {item.creator?.display_name ?? '未知作者'}
          {'　'}
          {duration(item.duration_ms)}
          {'　'}
          发布于 {date(item.published_at_ms)}
          {'　'}
          存于 {date(item.saved_at_ms)}
        </p>
      </div>

      {item.processing.excluded && (
        <p className="notice">
          这条已被处理策略排除：它的证据和结论仍然保存在库里，但不会出现在检索、回答和百科中。
          {item.policy.rule_name || item.policy.rule_id
            ? `　规则：${item.policy.rule_name ?? item.policy.rule_id}`
            : ''}
          　删除或停用该规则即可恢复，不需要重新处理。
        </p>
      )}

      <div className="figures">
        <div className="figure">
          <div className="figure__value">{statusLabel(item.processing.status)}</div>
          <div className="figure__label">处理状态</div>
        </div>
        <div className="figure">
          <div className="figure__value">
            L{item.processing.achieved_level}
            <span className="faint" style={{ fontSize: '0.9rem' }}>
              /{item.processing.desired_level}
            </span>
          </div>
          <div className="figure__label">已达深度 / 目标</div>
        </div>
        <div className="figure">
          <div className="figure__value">{item.evidence.length}</div>
          <div className="figure__label">证据片段</div>
        </div>
        <div className="figure">
          <div className="figure__value">{claims.length}</div>
          <div className="figure__label">结论</div>
        </div>
      </div>

      <div className="chips">
        <button
          className="btn"
          onClick={() => process.mutate(false)}
          disabled={process.isPending}
        >
          处理这条
        </button>
        <button
          className="btn btn--quiet"
          onClick={() => process.mutate(true)}
          disabled={process.isPending}
          title="重新跑一遍，旧的结果会被新一次运行取代，不会被删掉"
        >
          重新处理
        </button>
        {item.source_url ? (
          <a className="btn btn--quiet" href={item.source_url} target="_blank" rel="noreferrer">
            在抖音打开
          </a>
        ) : null}
        <button
          className="btn btn--quiet"
          onClick={() => remove.mutate()}
          disabled={remove.isPending}
          title="只在本地标记删除，原始收藏和证据都还在"
        >
          从库里移除
        </button>
      </div>

      {process.isSuccess ? <p className="notice">已排队，处理完这页会更新。</p> : null}
      {process.isError ? (
        <p className="notice notice--error">{errorMessage(process.error)}</p>
      ) : null}

      {latestDecision && latestDecision.action !== 'process' ? (
        <p className="notice notice--error">
          这条按规则被判为「{actionLabel(latestDecision.action)}」，所以没有做深度处理。
          原因：{latestDecision.reason_code ?? '未记录'}。去<Link to="/settings">设置</Link>
          里改规则。
        </p>
      ) : null}

      {claims.length > 0 ? (
        <section>
          <h2>结论</h2>
          <p className="muted" style={{ fontSize: '0.85rem', maxWidth: '68ch' }}>
            每条都带出处和说话的人，作者的说法和你自己的体验不会混在一起。
          </p>
          <table>
            <thead>
              <tr>
                <th>主体</th>
                <th>属性</th>
                <th>值</th>
                <th>说法来自</th>
                <th className="num">置信度</th>
              </tr>
            </thead>
            <tbody>
              {claims.map((claim) => (
                <tr key={claim.id}>
                  <td>{claim.subject ?? '—'}</td>
                  <td>{claim.predicate}</td>
                  <td>
                    {claim.value_text ??
                      (claim.value_number != null
                        ? `${claim.value_number}${claim.currency ?? claim.unit ?? ''}`
                        : '—')}
                  </td>
                  <td>{claim.attribution}</td>
                  <td className="num">
                    {claim.confidence != null ? claim.confidence.toFixed(2) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}

      <section>
        <h2>证据原文</h2>
        {item.evidence.length === 0 ? (
          <div className="empty">
            <h3>还没有提取出证据</h3>
            <p className="muted">点上面的「处理这条」，字幕和画面文字会被抽出来存在这里。</p>
          </div>
        ) : (
          <div className="evidence">
            {item.evidence.map((unit) => (
              <div className="evidence__unit" key={unit.id}>
                <div className="evidence__stamp">
                  {unit.start_ms != null ? timecode(unit.start_ms) : unit.kind}
                </div>
                <div className="evidence__text">{unit.text ?? '（空）'}</div>
              </div>
            ))}
          </div>
        )}
      </section>

      {item.runs.length > 0 ? (
        <section>
          <h2>处理记录</h2>
          <p className="muted" style={{ fontSize: '0.85rem', maxWidth: '68ch' }}>
            每次处理都是一个新版本，旧版本保留但不再被检索，所以重跑不会弄丢东西。
          </p>
          <table>
            <thead>
              <tr>
                <th className="num">深度</th>
                <th>结果</th>
                <th>时间</th>
                <th>在用</th>
              </tr>
            </thead>
            <tbody>
              {item.runs.map((run) => (
                <tr key={run.id}>
                  <td className="num">L{run.achieved_level ?? run.target_level}</td>
                  <td>{run.status}</td>
                  <td className="num">{dateTime(run.finished_at_ms ?? run.started_at_ms)}</td>
                  <td>{run.is_current ? '是' : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ) : null}
    </div>
  )
}
