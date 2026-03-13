import { type ReactNode, useEffect } from 'react'

type WindowProps = {
  title: string
  children?: ReactNode
}

const activeWindowTitles = new Set<string>()
let syncTimeout: number | null = null

function syncWindowsWithBackend() {
  if (syncTimeout !== null) {
    return
  }

  syncTimeout = window.setTimeout(() => {
    syncTimeout = null
    window.ipcRenderer.send('window:sync', Array.from(activeWindowTitles))
  }, 0)
}

const Window = ({ title }: WindowProps) => {
  useEffect(() => {
    activeWindowTitles.add(title)
    window.ipcRenderer.send('window:create', { title })
    syncWindowsWithBackend()

    return () => {
      activeWindowTitles.delete(title)
      window.ipcRenderer.send('window:close', title)
      syncWindowsWithBackend()
    }
  }, [title])

  return null
}

export default Window