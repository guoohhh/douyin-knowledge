// The client uses root-relative paths, which is correct: the bundle is served from the same
// origin as the API (or proxied there by Vite), so no base URL is ever baked in. Node has no
// origin, so one is supplied here rather than by making the client configurable for tests.
// 8787 is `Settings.api_port`, which is what `dk serve` binds and what the Vite proxy
// targets. This said 8797, so `npm run probe` could only ever fail to connect unless the
// operator happened to set DK_ORIGIN -- and the failure looked like "服务没启动" rather
// than a wrong port, which is the most misleading way for a harness to break.
const origin = process.env.DK_ORIGIN ?? 'http://127.0.0.1:8787'
const real = globalThis.fetch
globalThis.fetch = ((input: any, init?: any) =>
  real(typeof input === 'string' && input.startsWith('/') ? origin + input : input, init)) as typeof fetch
