import type { ConnectionStatus } from '@renderer/stores/useAppStore'

type StatusListener = (status: ConnectionStatus) => void
type MessageListener = (message: string) => void
type ErrorListener = (error: string | null) => void

const DEFAULT_WS_URL = 'ws://127.0.0.1:8765/ws'

class WebSocketManager {
  private socket: WebSocket | null = null
  private readonly url = import.meta.env.VITE_AIWS_URL ?? DEFAULT_WS_URL
  private readonly pendingMessages: string[] = []
  private readonly statusListeners = new Set<StatusListener>()
  private readonly messageListeners = new Set<MessageListener>()
  private readonly errorListeners = new Set<ErrorListener>()
  private reconnectTimer: number | null = null
  private reconnectAttempts = 0
  private manuallyDisconnected = false

  connect(): void {
    if (
      this.socket?.readyState === WebSocket.OPEN ||
      this.socket?.readyState === WebSocket.CONNECTING
    ) {
      return
    }

    this.manuallyDisconnected = false
    this.clearReconnectTimer()
    this.emitStatus('connecting')
    this.emitError(null)

    const socket = new WebSocket(this.url)
    this.socket = socket
    let opened = false

    socket.onopen = () => {
      if (this.socket !== socket) return

      opened = true
      this.reconnectAttempts = 0
      this.emitStatus('connected')
      this.emitError(null)

      while (this.pendingMessages.length > 0) {
        const nextMessage = this.pendingMessages.shift()
        if (nextMessage) {
          socket.send(nextMessage)
        }
      }
    }

    socket.onmessage = (event) => {
      this.messageListeners.forEach((listener) => listener(String(event.data)))
    }

    socket.onerror = () => {
      if (this.socket !== socket || opened) return

      this.emitStatus('error')
      this.emitError('Unable to reach the websocket server. Retrying...')
    }

    socket.onclose = () => {
      if (this.socket === socket) {
        this.socket = null
      }

      if (this.manuallyDisconnected) {
        this.emitStatus('disconnected')
        return
      }

      if (!opened) {
        this.emitStatus('error')
        this.emitError('Unable to reach the websocket server. Retrying...')
      } else {
        this.emitStatus('disconnected')
      }

      this.scheduleReconnect()
    }
  }

  disconnect(): void {
    this.manuallyDisconnected = true
    this.clearReconnectTimer()

    if (!this.socket) {
      this.emitStatus('disconnected')
      this.emitError(null)
      return
    }

    const socket = this.socket
    this.socket = null
    socket.close()
    this.emitStatus('disconnected')
    this.emitError(null)
  }

  send(message: string): void {
    const trimmedMessage = message.trim()
    if (!trimmedMessage) return

    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(trimmedMessage)
      return
    }

    this.pendingMessages.push(trimmedMessage)
    this.connect()
  }

  onStatusChange(listener: StatusListener): () => void {
    this.statusListeners.add(listener)
    return () => this.statusListeners.delete(listener)
  }

  onMessage(listener: MessageListener): () => void {
    this.messageListeners.add(listener)
    return () => this.messageListeners.delete(listener)
  }

  onError(listener: ErrorListener): () => void {
    this.errorListeners.add(listener)
    return () => this.errorListeners.delete(listener)
  }

  private scheduleReconnect(): void {
    if (this.manuallyDisconnected || this.reconnectTimer !== null) {
      return
    }

    const delay = Math.min(1000 * 2 ** this.reconnectAttempts, 5000)
    this.reconnectAttempts += 1
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }

  private emitStatus(status: ConnectionStatus): void {
    this.statusListeners.forEach((listener) => listener(status))
  }

  private emitError(error: string | null): void {
    this.errorListeners.forEach((listener) => listener(error))
  }
}

export const websocketManager = new WebSocketManager()
