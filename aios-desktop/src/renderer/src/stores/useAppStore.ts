import { create } from 'zustand'

export type MessageRole = 'user' | 'server' | 'system'
export type ConnectionStatus = 'idle' | 'connecting' | 'connected' | 'disconnected' | 'error'

export interface AppMessage {
  id: string
  role: MessageRole
  content: string
}

interface AppState {
  input: string
  messages: AppMessage[]
  connectionStatus: ConnectionStatus
  connectionError: string | null
  setInput: (input: string) => void
  addMessage: (role: MessageRole, content: string) => void
  setConnectionStatus: (status: ConnectionStatus) => void
  setConnectionError: (error: string | null) => void
  clearMessages: () => void
}

export const useAppStore = create<AppState>()((set) => ({
  input: '',
  messages: [],
  connectionStatus: 'idle',
  connectionError: null,
  setInput: (input) => set({ input }),
  addMessage: (role, content) =>
    set((state) => ({
      messages: [...state.messages, { id: crypto.randomUUID(), role, content }]
    })),
  setConnectionStatus: (connectionStatus) => set({ connectionStatus }),
  setConnectionError: (connectionError) => set({ connectionError }),
  clearMessages: () => set({ messages: [] })
}))
