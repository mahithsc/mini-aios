import { ElectronAPI } from '@electron-toolkit/preload'

interface WindowApi {
  setWindowClickable: (clickable: boolean) => void
}

declare global {
  interface Window {
    electron: ElectronAPI
    api: WindowApi
  }
}
