import { create } from 'zustand'
import type { User } from '../types'
import { auth } from '../services/api'

interface AuthState {
  user: User | null
  token: string | null
  isAuthenticated: boolean
  isLoading: boolean

  login: (email: string, password: string) => Promise<void>
  register: (email: string, displayName: string, password: string) => Promise<void>
  logout: () => void
  loadUser: () => Promise<void>
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  token: localStorage.getItem('token'),
  isAuthenticated: !!localStorage.getItem('token'),
  isLoading: false,

  login: async (email, password) => {
    const result = await auth.login(email, password)
    localStorage.setItem('token', result.access_token)
    set({ token: result.access_token, isAuthenticated: true })
    const user = await auth.me() as User
    set({ user })
  },

  register: async (email, displayName, password) => {
    const result = await auth.register(email, displayName, password)
    localStorage.setItem('token', result.access_token)
    set({ token: result.access_token, isAuthenticated: true })
    const user = await auth.me() as User
    set({ user })
  },

  logout: () => {
    localStorage.removeItem('token')
    set({ user: null, token: null, isAuthenticated: false })
  },

  loadUser: async () => {
    set({ isLoading: true })
    try {
      const user = await auth.me() as User
      set({ user, isLoading: false })
    } catch {
      localStorage.removeItem('token')
      set({ user: null, token: null, isAuthenticated: false, isLoading: false })
    }
  },
}))
