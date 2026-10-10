import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { api } from '../src/api/client'
import type { CaptureScope, CaptureTargets, NamedCollectionTarget } from '../src/api/types'
import { canSyncScope, copyScope, scopeChanged, syncAcceptanceMessage, toggleNamed, visibleNamedTargets } from '../src/lib/captureScope'

// The Collections browser must navigate to the single explicit sync control.
const collectionsPage = readFileSync('src/pages/CollectionsPage.tsx', 'utf8')
assert.match(collectionsPage, /to="\/settings#capture-scope-title"/)
assert.doesNotMatch(collectionsPage, /api\.sync\(|auto_process:\s*true/)

const empty: CaptureScope = {
  schema_version: 1, platform: 'douyin', default_favorites: false, named_collection_ids: [],
}
const named = (id: string, selected = false): NamedCollectionTarget => ({
  external_collection_id: id,
  name: id,
  item_count: 2,
  discovered: true,
  locally_observed: false,
  selected,
  last_synced_at_ms: null,
})

// Fresh real account: no implicit selection, and an empty saved scope cannot sync.
assert.equal(canSyncScope(empty, copyScope(empty)), false)
assert.equal(scopeChanged(empty, copyScope(empty)), false)
assert.deepEqual(visibleNamedTargets(empty, []), [])

// Single, multiple, default-only, mixed, and explicitly empty drafts.
const one = toggleNamed(empty, 'A')
const multiple = toggleNamed(one, 'B')
assert.deepEqual(multiple.named_collection_ids, ['A', 'B'])
assert.equal(canSyncScope(empty, multiple), false, 'unsaved edits must block Sync now')
assert.equal(canSyncScope(multiple, copyScope(multiple)), true)
assert.equal(canSyncScope({ ...empty, default_favorites: true }, { ...empty, default_favorites: true }), true)
assert.equal(canSyncScope({ ...multiple, default_favorites: true }, { ...multiple, default_favorites: true }), true)
assert.deepEqual(toggleNamed(toggleNamed(multiple, 'A'), 'B').named_collection_ids, [])
assert.equal(canSyncScope(empty, empty), false)

// Discovery failure or temporary disappearance cannot erase a saved ID.
const saved = { ...empty, named_collection_ids: ['missing', 'A'] }
const rows = visibleNamedTargets(saved, [named('A', true), named('B')])
assert.deepEqual(rows.map((row) => row.external_collection_id), ['A', 'B', 'missing'])
assert.equal(rows.find((row) => row.external_collection_id === 'missing')?.selected, true)
assert.equal(rows.find((row) => row.external_collection_id === 'missing')?.discovered, false)
assert.equal(rows.find((row) => row.external_collection_id === 'B')?.selected, false)
assert.deepEqual(visibleNamedTargets(saved, []).map((row) => row.external_collection_id), ['A', 'missing'])

let persisted = copyScope(empty)
let syncCalls = 0
let reuseSyncJob = false
const requests: Array<{ path: string; method: string; body: unknown }> = []
const targets: CaptureTargets = {
  scope: persisted,
  discovery: { state: 'error', error_code: 'capture_unavailable', message: 'Named collection discovery failed' },
  default_favorites: { available: true, selected: false, last_completed_at_ms: null },
  named_collections: [],
}

globalThis.fetch = async (input, init) => {
  const path = String(input)
  const method = init?.method ?? 'GET'
  const body = init?.body ? JSON.parse(String(init.body)) as unknown : null
  requests.push({ path, method, body })
  let response: unknown
  if (path === '/api/sources/capture-scope' && method === 'GET') response = persisted
  else if (path === '/api/sources/capture-scope' && method === 'PUT') {
    persisted = body as CaptureScope
    response = persisted
  } else if (path === '/api/sources/capture-scope/targets') response = targets
  else if (path === '/api/sources/sync') {
    syncCalls++
    response = {
      job_id: reuseSyncJob ? 'job_old' : 'job_one', job_type: 'sync_capture_scope',
      status: reuseSyncJob ? 'running' : 'queued', created: !reuseSyncJob,
    }
  } else if (path === '/api/sources/src_one/process') {
    response = { job_id: 'job_two', job_type: 'process_source', status: 'queued', created: false }
  } else throw new Error(`Unexpected request ${method} ${path}`)
  return new Response(JSON.stringify(response), {
    status: path === '/api/sources/sync' || path.endsWith('/process') ? 202 : 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

assert.deepEqual(await api.captureScope(), empty)
const discovered = await api.captureTargets()
assert.equal(discovered.discovery.state, 'error', 'HTTP 200 can carry discovery failure')
assert.equal(discovered.default_favorites.available, true)
assert.equal(syncCalls, 0)

// Saving uses only PUT, then a fresh GET returns the persisted response.
const selected = { ...empty, default_favorites: true, named_collection_ids: ['A', 'B'] }
assert.deepEqual(await api.saveCaptureScope(selected), selected)
assert.deepEqual(await api.captureScope(), selected)
assert.equal(syncCalls, 0, 'Save must not auto-sync')
assert.deepEqual(requests.filter((r) => r.method === 'PUT').map((r) => r.path), [
  '/api/sources/capture-scope',
])

// JobAccepted is the backend shape. Acceptance is distinct from completion.
const accepted = await api.sync({ auto_process: false })
assert.deepEqual(accepted, {
  job_id: 'job_one', job_type: 'sync_capture_scope', status: 'queued', created: true,
})
assert.deepEqual(requests.find((r) => r.path === '/api/sources/sync')?.body, { auto_process: false })
assert.match(syncAcceptanceMessage(accepted), /新同步任务已受理/)
assert.match(syncAcceptanceMessage(accepted), /不代表同步完成/)

await api.sync({ auto_process: true })
assert.deepEqual(requests.filter((r) => r.path === '/api/sources/sync').at(-1)?.body,
  { auto_process: true })
reuseSyncJob = true
const reusedSync = await api.sync({ auto_process: false })
assert.equal(reusedSync.created, false)
assert.equal(reusedSync.status, 'running')
assert.match(syncAcceptanceMessage(reusedSync), /本次 AI 处理勾选不会更改该任务原有的处理选项/)
assert.match(syncAcceptanceMessage(reusedSync), /复用不代表同步完成/)
const reused = await api.processSource('src_one')
assert.equal(reused.created, false)
assert.equal(reused.status, 'queued')

console.log('Capture Scope state and API contract: passed')
