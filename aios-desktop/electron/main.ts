import { app, BrowserWindow, ipcMain } from 'electron'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

// The built directory structure
//
// ├─┬─┬ dist
// │ │ └── index.html
// │ │
// │ ├─┬ dist-electron
// │ │ ├── main.js
// │ │ └── preload.mjs
// │
process.env.APP_ROOT = path.join(__dirname, '..')

// 🚧 Use ['ENV_NAME'] avoid vite:define plugin - Vite@2.x
export const VITE_DEV_SERVER_URL = process.env['VITE_DEV_SERVER_URL']
export const MAIN_DIST = path.join(process.env.APP_ROOT, 'dist-electron')
export const RENDERER_DIST = path.join(process.env.APP_ROOT, 'dist')

process.env.VITE_PUBLIC = VITE_DEV_SERVER_URL ? path.join(process.env.APP_ROOT, 'public') : RENDERER_DIST

let mainWindow: BrowserWindow | null = null
const childWindows = new Map<string, BrowserWindow>()

function loadRenderer(window: BrowserWindow, windowType: 'main' | 'child', title?: string) {
  if (VITE_DEV_SERVER_URL) {
    const url = new URL(VITE_DEV_SERVER_URL)
    url.searchParams.set('window', windowType)
    if (title) {
      url.searchParams.set('title', title)
    }
    window.loadURL(url.toString())
    return
  }

  window.loadFile(path.join(RENDERER_DIST, 'index.html'), {
    query: {
      window: windowType,
      title: title ?? '',
    },
  })
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    show: false,
    icon: path.join(process.env.VITE_PUBLIC, 'electron-vite.svg'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.mjs'),
    },
  })

  // Test active push message to Renderer-process.
  mainWindow.webContents.on('did-finish-load', () => {
    mainWindow?.webContents.send('main-process-message', (new Date).toLocaleString())
  })

  loadRenderer(mainWindow, 'main')
}

function createChildWindow(title: string) {
  const key = title
  const existingWindow = childWindows.get(key)
  if (existingWindow && !existingWindow.isDestroyed()) {
    existingWindow.focus()
    return
  }

  const childWindow = new BrowserWindow({
    title,
    icon: path.join(process.env.VITE_PUBLIC, 'electron-vite.svg'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.mjs'),
    },
  })

  childWindows.set(key, childWindow)
  childWindow.on('closed', () => {
    childWindows.delete(key)
  })

  loadRenderer(childWindow, 'child', title)
}

function closeChildWindow(title: string) {
  const key = title
  const existingWindow = childWindows.get(key)
  if (!existingWindow || existingWindow.isDestroyed()) {
    childWindows.delete(key)
    return
  }
  childWindows.delete(key)
  existingWindow.close()
}

// Quit when all windows are closed, except on macOS. There, it's common
// for applications and their menu bar to stay active until the user quits
// explicitly with Cmd + Q.
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
    mainWindow = null
  }
})

app.on('activate', () => {
  // On OS X it's common to re-create a window in the app when the
  // dock icon is clicked and there are no other windows open.
  if (BrowserWindow.getAllWindows().length === 0) {
    createMainWindow()
  }
})

ipcMain.on('window:create', (_event, payload: { title: string }) => {
  if (!payload?.title) {
    return
  }
  createChildWindow(payload.title)
})

ipcMain.on('window:close', (_event, title: string) => {
  if (!title) {
    return
  }
  closeChildWindow(title)
})

app.whenReady().then(() => createMainWindow())
