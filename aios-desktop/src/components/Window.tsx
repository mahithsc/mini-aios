import { type ReactNode, useEffect } from 'react'

type WindowProps = {
  title: string
  children?: ReactNode
}

const Window = ({ title }: WindowProps) => {
  useEffect(() => {
    window.ipcRenderer.send('window:create', { title })

    return () => {
      window.ipcRenderer.send('window:close', title)
    }
  }, [title])

  return null
}

export default Window