// The client uses root-relative paths, which is correct: the bundle is served from the same
// origin as the API (or proxied there by Vite), so no base URL is ever baked in. Node has no
// origin, so one is supplied here rather than by making the client configurable for tests.
const origin = process.env.DK_ORIGIN ?? 'http://127.0.0.1:8797'
const real = globalThis.fetch
globalThis.fetch = ((input: any, init?: any) =>
  real(typeof input === 'string' && input.startsWith('/') ? origin + input : input, init)) as typeof fetch
