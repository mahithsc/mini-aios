import { useAppStore } from '@renderer/stores/useAppStore'
import React from 'react'
import TextInput from './TextInput'

const AnswerPanel: React.FC = () => {
  const messages = useAppStore((s) => s.messages)
  const connectionStatus = useAppStore((s) => s.connectionStatus)
  const connectionError = useAppStore((s) => s.connectionError)

  return (
    <div>
      <div className="px-2 pb-3">
        <div className="mb-2 text-xs uppercase tracking-wide text-white/60">
          WebSocket: {connectionStatus}
        </div>
        {connectionError ? (
          <div className="mb-2 text-sm text-red-300">{connectionError}</div>
        ) : null}
        {messages.map((msg) => (
          <div key={msg.id} className={msg.role === 'user' ? 'text-white' : 'text-white/75'}>
            <span className="mr-2 text-white/40">{msg.role === 'user' ? 'You' : 'Server'}</span>
            {msg.content}
          </div>
        ))}
      </div>
      <TextInput />
    </div>
  )
}

export default AnswerPanel
