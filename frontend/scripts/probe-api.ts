// Drives the real client module against a live server. curl proves the routes exist;
// this proves the code the browser runs agrees with them -- every payload-shape bug in
// this project came from a plausible guess, not from a missing endpoint.
//
// Run against a populated server:
//   DK_ORIGIN=http://127.0.0.1:8787 npm run probe
import './probe-origin'
import { api, ApiError } from '../src/api/client'

const need = (cond: unknown, what: string) => {
  if (!cond) { console.error('FAIL ' + what); process.exitCode = 1 } else console.log('ok   ' + what)
}

const run = async () => {
  const health = await api.health()
  need(health.status === 'ok', 'health')
  const { collections } = await api.collections()
  need(collections.length === 3 && collections[0].name, 'collections carry names')
  const list = await api.sources({ limit: 3 })
  need(list.sources.length === 3 && list.total >= 8, 'sources list')
  const detail = await api.source(list.sources[0].id)
  need(Array.isArray(detail.evidence) && detail.runs.length > 0, 'source detail spine')
  const search = await api.search('茶餐厅', 3)
  need(search.results.length > 0 && search.results[0].source_title !== undefined, 'search')
  const wiki = await api.wiki({ limit: 5 })
  need(wiki.pages.length > 0, 'wiki list')
  const page = await api.wikiPage(wiki.pages[0].id)
  need(page.supports.length > 0, 'wiki page has supports')
  need(page.supports.every((s) => s.resolved_source_id), 'every support resolves to a source')
  const turn = await api.ask({ query: '我收藏里有哪家茶餐厅' })
  need(typeof turn.content === 'string' && turn.conversation_id.startsWith('cnv_'), 'ask')
  const follow = await api.ask({ query: '人均多少', conversation_id: turn.conversation_id })
  need(follow.conversation_id === turn.conversation_id, 'follow-up stays in the thread')
  const scoped = await api.ask({ query: '珠峰多高', scope_override: 'general' })
  need(scoped.scope === 'general', 'scope override reaches the backend')
  const stats = await api.stats()
  need(stats.corpus && stats.demo_mode === true, 'stats')
  const settings = await api.settings()
  need(typeof settings.openai_api_key === 'boolean', 'settings never leak the key value')
  const { rules } = await api.rules()
  need(Array.isArray(rules), 'policy rules')
  const { decisions } = await api.decisions({ limit: 5 })
  need(decisions.length > 0 && decisions[0].phase !== undefined, 'policy decisions')
  try { await api.source('src_missing'); need(false, '404 raises') }
  catch (e) { need(e instanceof ApiError && (e as ApiError).status === 404, '404 becomes ApiError') }
}
run().catch((e) => { console.error('THREW', e); process.exitCode = 1 })
