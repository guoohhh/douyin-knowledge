import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../api/client'
import type { AIChoices, AIConfiguration, AIRole } from '../api/types'
import { errorMessage } from '../lib/format'

const ROLE_LABELS: Record<AIRole, string> = {
  triage: '内容判断',
  extraction: '知识提取',
  answer: '问答',
  asr: '语音转文字',
  embedding: '语义检索',
}
const ROLES = Object.keys(ROLE_LABELS) as AIRole[]

function providerFor(choices: AIChoices, role: AIRole): string {
  if (role === 'asr') return choices.asr_provider
  if (role === 'embedding') return choices.embedding_provider
  return choices.ai_provider
}

function alignModels(choices: AIChoices, view: AIConfiguration): AIChoices {
  const next = { ...choices,
    asr_provider: view.model_options.asr[choices.asr_provider] ? choices.asr_provider : 'mock' as const,
    embedding_provider: view.model_options.embedding[choices.embedding_provider] ? choices.embedding_provider : 'mock' as const,
  }
  const models = { ...next.models }
  for (const role of ROLES) {
    const options = view.model_options[role][providerFor(next, role)] ?? []
    if (!options.includes(models[role])) models[role] = options[0] ?? ''
  }
  return { ...next, models }
}

export function AISettingsSection() {
  const client = useQueryClient()
  const query = useQuery({ queryKey: ['ai-configuration'], queryFn: api.aiConfiguration })
  const [choices, setChoices] = useState<AIChoices | null>(null)
  const [credentials, setCredentials] = useState({ openai: '', deepseek: '', doubao: '' })

  useEffect(() => {
    if (query.data && choices === null) setChoices(alignModels(query.data.saved, query.data))
  }, [query.data, choices])

  const save = useMutation({
    mutationFn: () => {
      if (!choices) throw new Error('配置尚未加载')
      return api.saveAIConfiguration({
        ...choices,
        credentials: Object.fromEntries(
          Object.entries(credentials).filter(([, value]) => value.trim()),
        ),
      })
    },
    onSuccess: (result) => {
      setCredentials({ openai: '', deepseek: '', doubao: '' })
      setChoices(alignModels(result.saved, result))
      client.setQueryData(['ai-configuration'], result)
    },
  })

  if (query.isPending) return <section><h2>AI 提供商</h2><p className="muted">读取中…</p></section>
  if (query.isError) return <section><h2>AI 提供商</h2><p className="notice notice--error">{errorMessage(query.error)}</p></section>
  const view = query.data
  if (!choices) return null

  const updateProvider = (field: 'ai_provider' | 'asr_provider' | 'embedding_provider', value: string) => {
    setChoices(alignModels({ ...choices, [field]: value }, view))
  }

  return (
    <section>
      <h2>AI 提供商</h2>
      <p className="muted" style={{ maxWidth: '68ch' }}>
        新配置保存后需重启本地 API 和 worker 才会生效。运行中的任务继续使用启动时的配置。
        收藏同步不会自动调用付费 AI。手动处理视频和使用真实模型问答都可能产生费用；模拟模式不会产生外部 AI 费用。
      </p>

      <div className="chips">
        <label className="field"><span>文字处理</span>
          <select value={choices.ai_provider} onChange={(e) => updateProvider('ai_provider', e.target.value)}>
            <option value="mock">模拟（免费）</option><option value="openai">OpenAI</option><option value="deepseek">DeepSeek</option>
          </select>
        </label>
        <label className="field"><span>语音转文字</span>
          <select value={choices.asr_provider} onChange={(e) => updateProvider('asr_provider', e.target.value)}>
            <option value="mock">模拟（免费）</option><option value="openai">OpenAI Whisper</option><option value="doubao">豆包 ASR</option>
          </select>
        </label>
        <label className="field"><span>语义检索</span>
          <select value={choices.embedding_provider} onChange={(e) => updateProvider('embedding_provider', e.target.value)}>
            <option value="mock">模拟（免费）</option><option value="openai">OpenAI</option>
          </select>
        </label>
      </div>

      <div className="chips" style={{ marginTop: '0.75rem' }}>
        {ROLES.map((role) => (
          <label className="field" key={role}><span>{ROLE_LABELS[role]}</span>
            <select value={choices.models[role]} onChange={(e) => setChoices({ ...choices, models: { ...choices.models, [role]: e.target.value } })}>
              {(view.model_options[role][providerFor(choices, role)] ?? []).map((model) =>
                <option key={model} value={model}>{model}</option>,
              )}
            </select>
          </label>
        ))}
      </div>

      <p className="faint" style={{ marginTop: '0.8rem' }}>密钥只显示配置状态；留空表示保留现有密钥。环境变量优先于此处保存的密钥。</p>
      <div className="chips">
        {(['openai', 'deepseek', 'doubao'] as const).map((provider) => (
          <label className="field" key={provider}>
            <span>{provider === 'doubao' ? '豆包 ASR' : provider}：{view.credentials_configured[provider] ? '密钥已配置' : '密钥未配置'}{view.sdk_available[provider] ? '' : ' · 需安装适配器'}</span>
            <input type="password" autoComplete="new-password" value={credentials[provider]}
              placeholder="输入新密钥（不会回显）"
              onChange={(e) => setCredentials({ ...credentials, [provider]: e.target.value })} />
          </label>
        ))}
      </div>

      <button className="btn" style={{ marginTop: '0.85rem' }} disabled={save.isPending} onClick={() => save.mutate()}>保存 AI 配置</button>
      {save.isError && <p className="notice notice--error">{errorMessage(save.error)}</p>}
      {save.isSuccess && <p className="notice">已保存。请重启 API 和 worker 后再处理视频。</p>}
      {view.restart_required && <p className="notice">已保存的选择尚未生效；当前运行中的服务仍使用旧配置。</p>}
      {!view.sdk_available.openai && <p className="faint">使用 OpenAI 或 DeepSeek 前，需在 backend 安装对应可选依赖：<code>pip install -e '.[openai]'</code>。</p>}
      <p className="faint">当前生效：文字 {view.effective.ai_provider}，语音 {view.effective.asr_provider}，检索 {view.effective.embedding_provider}。</p>
      <p className="faint">已有向量索引时，切换语义检索模型需要单独迁移索引；系统会阻止直接处理，避免意外对整个语料调用付费 embedding。</p>
      <p className="faint">画面 OCR / 深度视觉处理尚未接入真实处理流水线，界面不会将其作为可用模型选项。</p>
    </section>
  )
}
