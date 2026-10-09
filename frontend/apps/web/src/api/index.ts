/**
 * HTTP client for the Flask backend.
 *
 * Contract (docs/API_CONTRACT.md):
 * - every JSON response is an envelope: `{ ok: true, data }` or `{ ok: false, message }`;
 * - non-GET requests must carry the session's CSRF token in `X-CSRF-Token`;
 * - field-facing writes are idempotent: the client sends an `Idempotency-Key`
 *   and keeps the same key for a retry after a network failure or a 5xx, so
 *   a scanner that double-submits (or a flaky LAN) replays instead of
 *   duplicating stock. A 4xx is a definitive rejection with nothing written,
 *   so the key is dropped and the next attempt starts fresh;
 * - 401 means the session is gone (back to the login page), 428 means the
 *   account must change its initial password first.
 *
 * This mirrors the behaviour of the previous vanilla-JS client one-for-one.
 */
import type { AxiosRequestConfig, Method } from 'axios'
import axios from 'axios'
import { reactive } from 'vue'

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * What a list response said about the window it returned.
 *
 * The list endpoints answer with a plain array and put the counts in headers, so
 * a caller that ignores them sees no difference from before — which is exactly
 * how an operator ends up looking at "the records" when it is really the newest
 * 500 of 30,000. `ListWindowNotice` reads this so the page can say so.
 */
export interface ListWindowInfo {
  total: number
  returned: number
  limit: number
}

const listWindows = reactive<Record<string, ListWindowInfo>>({})

function listWindowKey(url: string) {
  return url.replace(/^\//, '')
}

function recordListWindow(url: string, headers: Record<string, unknown>) {
  const total = headers['x-total-count']
  if (total === undefined) {
    return
  }
  listWindows[listWindowKey(url)] = {
    total: Number(total),
    returned: Number(headers['x-returned-count'] ?? 0),
    limit: Number(headers['x-list-limit'] ?? 0),
  }
}

/** The window the last response for this URL reported, if it reported one. */
export function listWindowFor(url: string): ListWindowInfo | undefined {
  return listWindows[listWindowKey(url)]
}

export interface RequestOptions {
  /** Query string parameters. Empty strings / null / undefined are dropped. */
  params?: Record<string, unknown>
  /** JSON body, or a FormData for uploads. */
  data?: unknown
  /** Server-side idempotency scope, e.g. `scan-gun.inbound`. */
  idempotent?: string
  /** Do not redirect to the login page on 401 (used by the session probe). */
  silentAuth?: boolean
  headers?: Record<string, string>
  signal?: AbortSignal
}

const NETWORK_ERROR_MESSAGE = '网络连接失败，请检查网络后重试'

// ---- Idempotency keys ------------------------------------------------------

const pendingIdempotencyKeys = new Map<string, string>()

function newIdempotencyKey() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `pts-${crypto.randomUUID()}`
  }
  return `pts-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`
}

export function idempotencyKeyFor(scope: string) {
  let key = pendingIdempotencyKeys.get(scope)
  if (!key) {
    key = newIdempotencyKey()
    pendingIdempotencyKeys.set(scope, key)
  }
  return key
}

export function clearIdempotencyKey(scope: string) {
  pendingIdempotencyKeys.delete(scope)
}

// ---- Session hooks (wired by the account store) ----------------------------

interface SessionHooks {
  csrfToken: () => string
  onUnauthorized: (message: string) => void
  onPasswordChangeRequired: () => void
}

let sessionHooks: SessionHooks = {
  csrfToken: () => '',
  onUnauthorized: () => {},
  onPasswordChangeRequired: () => {},
}

export function registerSessionHooks(hooks: SessionHooks) {
  sessionHooks = hooks
}

// ---- Core request ----------------------------------------------------------

const http = axios.create({
  baseURL: import.meta.env.VITE_APP_API_BASEURL || '/',
  timeout: 1000 * 60,
  withCredentials: true,
  // Every status is handled below so that envelope, idempotency and session
  // rules live in exactly one place.
  validateStatus: () => true,
})

function cleanParams(params?: Record<string, unknown>) {
  if (!params) {
    return undefined
  }
  const result: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') {
      continue
    }
    result[key] = value
  }
  return result
}

export async function request<T = unknown>(method: Method, url: string, options: RequestOptions = {}): Promise<T> {
  const { idempotent, silentAuth, params, data, signal } = options
  const headers: Record<string, string> = { Accept: 'application/json', ...options.headers }
  const upperMethod = method.toUpperCase()
  if (upperMethod !== 'GET') {
    const token = sessionHooks.csrfToken()
    if (token) {
      headers['X-CSRF-Token'] = token
    }
  }
  if (idempotent) {
    headers['Idempotency-Key'] = idempotencyKeyFor(idempotent)
  }
  const config: AxiosRequestConfig = {
    method: upperMethod as Method,
    url: url.replace(/^\//, ''),
    params: cleanParams(params),
    data,
    headers,
    signal,
    responseType: 'json',
  }
  let response
  try {
    response = await http.request(config)
  }
  catch (error) {
    if (axios.isCancel(error)) {
      throw error
    }
    // The request may well have reached the server before the connection
    // broke: keep the idempotency key so the operator's retry replays.
    throw new ApiError(NETWORK_ERROR_MESSAGE, 0)
  }
  const contentType = String(response.headers['content-type'] ?? '')
  const payload = contentType.includes('application/json') && response.data && typeof response.data === 'object'
    ? response.data as { ok?: boolean, data?: T, message?: string }
    : null
  const status = response.status
  if (status < 200 || status >= 300 || payload?.ok === false) {
    if (idempotent && status >= 400 && status < 500) {
      clearIdempotencyKey(idempotent)
    }
    const message = payload?.message || `请求失败（${status}）`
    if (status === 401 && !silentAuth) {
      sessionHooks.onUnauthorized(message)
    }
    if (status === 428) {
      sessionHooks.onPasswordChangeRequired()
    }
    throw new ApiError(message, status)
  }
  if (idempotent) {
    clearIdempotencyKey(idempotent)
  }
  if (upperMethod === 'GET') {
    recordListWindow(url, response.headers as Record<string, unknown>)
  }
  return (payload?.data ?? null) as T
}

export const api = {
  get: <T = unknown>(url: string, options?: RequestOptions) => request<T>('GET', url, options),
  post: <T = unknown>(url: string, data?: unknown, options?: RequestOptions) => request<T>('POST', url, { ...options, data }),
  put: <T = unknown>(url: string, data?: unknown, options?: RequestOptions) => request<T>('PUT', url, { ...options, data }),
  delete: <T = unknown>(url: string, options?: RequestOptions) => request<T>('DELETE', url, options),
}

/** Message for a caught error, whatever threw it. */
export function errorMessage(error: unknown, fallback = '操作失败') {
  if (error instanceof Error && error.message) {
    return error.message
  }
  if (typeof error === 'string' && error) {
    return error
  }
  return fallback
}

export default api
