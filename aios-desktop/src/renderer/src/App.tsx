import { websocketManager } from '@renderer/services/websocketManager'
import { useAppStore } from '@renderer/stores/useAppStore'
import { useEffect } from 'react'
import MainWindow from './windows/main-window/MainWindow'

const App: React.FC = () => {
  const addMessage = useAppStore((s) => s.addMessage)
  const setConnectionError = useAppStore((s) => s.setConnectionError)
  const setConnectionStatus = useAppStore((s) => s.setConnectionStatus)

  useEffect(() => {
    const unsubscribeStatus = websocketManager.onStatusChange(setConnectionStatus)
    const unsubscribeMessage = websocketManager.onMessage((message) =>
      addMessage('server', message)
    )
    const unsubscribeError = websocketManager.onError(setConnectionError)

    websocketManager.connect()

    return () => {
      unsubscribeStatus()
      unsubscribeMessage()
      unsubscribeError()
    }
  }, [addMessage, setConnectionError, setConnectionStatus])

  return <MainWindow />
}

export default App
