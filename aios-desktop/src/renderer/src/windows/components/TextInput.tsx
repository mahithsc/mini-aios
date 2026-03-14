import HoverClickableWrapper from '@renderer/components/HoverClickableWrapper'
import { websocketManager } from '@renderer/services/websocketManager'
import { useAppStore } from '@renderer/stores/useAppStore'
import React, { useRef, useCallback } from 'react'

const TextInput: React.FC = () => {
  const input = useAppStore((s) => s.input)
  const setInput = useAppStore((s) => s.setInput)
  const addMessage = useAppStore((s) => s.addMessage)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const resizeTextarea = useCallback(() => {
    const ta = textareaRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${ta.scrollHeight}px`
  }, [])

  const handleSubmit = useCallback(() => {
    const trimmed = input.trim()
    if (!trimmed) return

    addMessage('user', trimmed)
    websocketManager.send(trimmed)
    setInput('')
    requestAnimationFrame(resizeTextarea)
  }, [input, addMessage, resizeTextarea, setInput])

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      setInput(e.target.value)
      resizeTextarea()
    },
    [setInput, resizeTextarea]
  )

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        handleSubmit()
      }
    },
    [handleSubmit]
  )

  return (
    <HoverClickableWrapper className="w-fit">
      <div className="w-90 min-h-10 bg-[rgb(33,33,33)]/90 rounded-3xl flex items-center px-4 py-2">
        <textarea
          ref={textareaRef}
          value={input}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          className="w-full text-white resize-none bg-transparent leading-normal"
          rows={1}
        />
      </div>
    </HoverClickableWrapper>
  )
}

export default TextInput
