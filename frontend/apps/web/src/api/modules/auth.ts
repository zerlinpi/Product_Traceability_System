import type { AnyRecord, Role, SessionUser } from '../types'
import api from '../index'

/** Body of POST /api/users. New accounts must change the password at first login. */
export interface UserCreatePayload {
  username: string
  displayName: string
  password: string
  role: Role
}

/**
 * Body of PUT /api/users/:id. Absent fields keep their stored value; a
 * password resets the account (it must be changed again at next login).
 * The username is immutable.
 */
export interface UserUpdatePayload {
  displayName?: string
  role?: Role
  active?: boolean
  password?: string
}

export default {
  /** GET /api/health — public liveness probe. */
  health: () => api.get<{ status: string, time: string }>('/api/health', { silentAuth: true }),

  /** GET /api/auth/me — current session user (401 when not logged in). */
  me: () => api.get<SessionUser>('/api/auth/me', { silentAuth: true }),

  /** POST /api/auth/login */
  login: (data: { username: string, password: string }) => api.post<SessionUser>('/api/auth/login', data, { silentAuth: true }),

  /** POST /api/auth/logout */
  logout: () => api.post<null>('/api/auth/logout', {}, { silentAuth: true }),

  /** POST /api/auth/change-password — also clears the forced-change flag. */
  changePassword: (data: { currentPassword: string, newPassword: string }) => api.post<SessionUser>('/api/auth/change-password', data),

  /** GET /api/users (ADMIN) */
  users: () => api.get<SessionUser[]>('/api/users'),

  /** POST /api/users (ADMIN) */
  createUser: (data: UserCreatePayload | AnyRecord) => api.post<SessionUser>('/api/users', data),

  /** PUT /api/users/:id (ADMIN) */
  updateUser: (id: number, data: UserUpdatePayload | AnyRecord) => api.put<SessionUser>(`/api/users/${id}`, data),

  /** PUT /api/users/:id { active } (ADMIN) — the server refuses to deactivate the caller. */
  setUserActive: (id: number, active: boolean) => api.put<SessionUser>(`/api/users/${id}`, { active }),
}
