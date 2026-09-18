import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { actionLabel, dateTime, errorMessage, ruleTypeLabel } from '../lib/format'

function Figures() {
  const stats = useQuery({ queryKey: ['stats'], queryFn: api.stats })
  if (!stats.data) return null
  const { corpus, knowledge, index, queue } = stats.data
  return (
    <div className="figures">
      <div className="figure">
        <div className="figure__value">
          {corpus.processed_sources}
          <span className="faint" style={{ fontSize: '0.9rem' }}>/{corpus.sources}</span>
        </div>
        {/* Processed over total rather than a bare total: the gap is the number that tells
            the user how much of their collection is actually answerable right now. */}
        <div className="figure__label">已处理 / 收藏总数</div>
      </div>
      <div className="figure">
        <div className="figure__value">{knowledge.entities}</div>
        <div className="figure__label">实体</div>
      </div>
      <div className="figure">
        <div className="figure__value">{knowledge.claims}</div>
        <div className="figure__label">结论</div>
      </div>
      <div className="figure">
        <div className="figure__value">{knowledge.wiki_pages}</div>
        <div className="figure__label">条目</div>
      </div>
      <div className="figure">
        <div className="figure__value">{index.vector_documents}</div>
        <div className="figure__label">已建向量索引</div>
      </div>
      <div className="figure">
        <div className="figure__value">{queue.runs_failed}</div>
        <div className="figure__label">失败的处理</div>
      </div>
    </div>
  )
}

function Models() {
  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  if (!settings.data) return null
  const { settings: values, resolved_models, demo_mode } = settings.data
  // `openai_api_key` arrives from the API as a boolean, never the value: the settings
  // endpoint can answer "is a key configured" but has no way to answer "with what".
  const hasKey = values['openai_api_key'] === true

  return (
    <section>
      <h2>模型</h2>
      <p className="muted" style={{ fontSize: '0.85rem', maxWidth: '68ch' }}>
        {demo_mode
          ? '现在用的是模拟模型，不联网、不花钱，回答内容只是占位。要真实结果就在 .env 里填 DK_OPENAI_API_KEY，然后重启服务。'
          : '已接入真实模型。每次处理都会把实际调用的模型名记进运行记录里。'}
      </p>
      <table>
        <thead>
          <tr>
            <th>用途</th>
            <th>模型</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(resolved_models).map(([role, model]) => (
            <tr key={role}>
              <td>{role}</td>
              <td className="mono">{model}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="faint" style={{ fontSize: '0.82rem', marginTop: '0.75rem' }}>
        API key {hasKey ? '已配置' : '未配置'}。密钥只存在 .env 里，界面和接口都不会显示它。
      </p>
    </section>
  )
}

function Rules() {
  const queryClient = useQueryClient()
  const rules = useQuery({ queryKey: ['rules'], queryFn: api.rules })
  const [form, setForm] = useState({ rule_type: 'creator', action: 'exclude', target: '', name: '' })

  const create = useMutation({
    mutationFn: () =>
      api.createRule({
        name: form.name || null,
        is_enabled: true,
        rule_type: form.rule_type,
        action: form.action,
        priority: 50,
        ...(form.rule_type === 'creator'
          ? { target_creator_id: form.target }
          : form.rule_type === 'collection'
            ? { target_collection_id: form.target }
            : { target_source_id: form.target }),
      }),
    onSuccess: () => {
      setForm({ ...form, target: '', name: '' })
      queryClient.invalidateQueries({ queryKey: ['rules'] })
    },
  })

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteRule(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['rules'] }),
  })

  const rows = rules.data?.rules ?? []

  return (
    <section>
      <h2>处理规则</h2>
      <p className="muted" style={{ fontSize: '0.85rem', maxWidth: '68ch' }}>
        不是所有收藏都值得花模型调用。规则在处理前生效，每次判断都会记下理由，
        所以某条为什么没被处理，永远查得到。
      </p>

      {rows.length === 0 ? (
        <p className="notice">还没有规则，所有收藏都会被正常处理。</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>规则</th>
              <th>范围</th>
              <th>动作</th>
              <th>目标</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((rule) => (
              <tr key={rule.id}>
                <td>{rule.name ?? '（未命名）'}</td>
                <td>{ruleTypeLabel(rule.rule_type)}</td>
                <td>{actionLabel(rule.action)}</td>
                <td className="mono">
                  {rule.target_creator_id ??
                    rule.target_collection_id ??
                    rule.target_source_id ??
                    '按条件匹配'}
                </td>
                <td>
                  <button
                    className="btn btn--quiet"
                    onClick={() => remove.mutate(rule.id)}
                    disabled={remove.isPending}
                  >
                    删除
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <form
        style={{ display: 'flex', gap: '0.6rem', flexWrap: 'wrap', marginTop: '1.25rem' }}
        onSubmit={(event) => {
          event.preventDefault()
          if (form.target.trim()) create.mutate()
        }}
      >
        <div className="field">
          <label htmlFor="rule-type">范围</label>
          <select
            id="rule-type"
            value={form.rule_type}
            onChange={(event) => setForm({ ...form, rule_type: event.target.value })}
          >
            <option value="creator">作者</option>
            <option value="collection">收藏夹</option>
            <option value="source">单个视频</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="rule-action">动作</label>
          <select
            id="rule-action"
            value={form.action}
            onChange={(event) => setForm({ ...form, action: event.target.value })}
          >
            <option value="exclude">不处理</option>
            <option value="metadata_only">只存元数据</option>
            <option value="always_process">总是深度处理</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="rule-target">目标 ID</label>
          <input
            id="rule-target"
            value={form.target}
            onChange={(event) => setForm({ ...form, target: event.target.value })}
            placeholder="cre_… / col_… / src_…"
            style={{ minWidth: '16rem' }}
          />
        </div>
        <div className="field">
          <label htmlFor="rule-name">备注</label>
          <input
            id="rule-name"
            value={form.name}
            onChange={(event) => setForm({ ...form, name: event.target.value })}
            placeholder="为什么加这条规则"
          />
        </div>
        <button className="btn" type="submit" disabled={create.isPending || !form.target.trim()}>
          添加规则
        </button>
      </form>
      {create.isError ? (
        <p className="notice notice--error">{errorMessage(create.error)}</p>
      ) : null}
    </section>
  )
}

function Decisions() {
  const decisions = useQuery({
    queryKey: ['decisions', 'recent'],
    queryFn: () => api.decisions({ limit: 20 }),
  })
  const rows = decisions.data?.decisions ?? []
  if (rows.length === 0) return null

  return (
    <section>
      <h2>最近的处理判断</h2>
      <table>
        <thead>
          <tr>
            <th>收藏</th>
            <th>判断</th>
            <th>理由</th>
            <th className="num">时间</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((decision) => (
            <tr key={decision.id}>
              <td>
                <Link className="mono" to={`/sources/${decision.source_id}`}>
                  {decision.source_id}
                </Link>
              </td>
              <td>{actionLabel(decision.action)}</td>
              <td className="mono">{decision.reason_code ?? '—'}</td>
              <td className="num">{dateTime(decision.created_at_ms)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

export function SettingsPage() {
  const queryClient = useQueryClient()
  const sync = useMutation({
    mutationFn: () => api.sync({ auto_process: true }),
    onSuccess: () => queryClient.invalidateQueries(),
  })
  const reindex = useMutation({
    mutationFn: () => api.reindex(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['stats'] }),
  })

  return (
    <div className="stack">
      <div>
        <h1>设置</h1>
        <p className="lede">全部数据都在这台机器上的一个 SQLite 文件里，没有账号，也不上传。</p>
      </div>

      <Figures />

      <div className="chips">
        <button className="btn" onClick={() => sync.mutate()} disabled={sync.isPending}>
          同步并处理
        </button>
        <button
          className="btn btn--quiet"
          onClick={() => reindex.mutate()}
          disabled={reindex.isPending}
          title="从证据重建关键词和向量索引，不会重新调用模型做抽取"
        >
          重建索引
        </button>
      </div>
      {sync.isSuccess || reindex.isSuccess ? (
        <p className="notice">已交给后台，处理进度看上面的数字。</p>
      ) : null}

      <Models />
      <Rules />
      <Decisions />
    </div>
  )
}
