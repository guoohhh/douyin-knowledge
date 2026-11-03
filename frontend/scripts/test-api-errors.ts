import assert from 'node:assert/strict'
import { api, ApiError } from '../src/api/client'

for (const [response, code, retryable, message] of [
  [new Response('Internal Server Error', { status: 500 }), 'http_error', false, '请求失败（500）'],
  [Response.json({ error: { code: 'model_unavailable', retryable: true, message: 'AI 服务暂时繁忙，请稍后重试' } },
    { status: 503 }), 'model_unavailable', true, 'AI 服务暂时繁忙，请稍后重试'],
] as const) {
  globalThis.fetch = async () => response
  await assert.rejects(api.health(), (error: unknown) =>
    error instanceof ApiError && error.status === response.status &&
    error.code === code && error.retryable === retryable && error.message === message)
}
console.log('API error contract passed')
