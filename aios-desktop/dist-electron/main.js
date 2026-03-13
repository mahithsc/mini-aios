import { app, BrowserWindow, ipcMain, screen } from "electron";
import { fileURLToPath } from "node:url";
import path from "node:path";
const __dirname$1 = path.dirname(fileURLToPath(import.meta.url));
process.env.APP_ROOT = path.join(__dirname$1, "..");
const VITE_DEV_SERVER_URL = process.env["VITE_DEV_SERVER_URL"];
const MAIN_DIST = path.join(process.env.APP_ROOT, "dist-electron");
const RENDERER_DIST = path.join(process.env.APP_ROOT, "dist");
process.env.VITE_PUBLIC = VITE_DEV_SERVER_URL ? path.join(process.env.APP_ROOT, "public") : RENDERER_DIST;
let mainWindow = null;
const childWindows = /* @__PURE__ */ new Map();
const CHILD_WINDOW_BASE_X = 120;
const CHILD_WINDOW_BASE_Y = 120;
const CHILD_WINDOW_OFFSET_X = 28;
const CHILD_WINDOW_OFFSET_Y = 28;
const CHILD_WINDOW_WIDTH = 800;
const CHILD_WINDOW_HEIGHT = 600;
let nextChildWindowSlot = 0;
function loadRenderer(window, windowType, title) {
  if (VITE_DEV_SERVER_URL) {
    const url = new URL(VITE_DEV_SERVER_URL);
    url.searchParams.set("window", windowType);
    if (title) {
      url.searchParams.set("title", title);
    }
    window.loadURL(url.toString());
    return;
  }
  window.loadFile(path.join(RENDERER_DIST, "index.html"), {
    query: {
      window: windowType,
      title: title ?? ""
    }
  });
}
function createMainWindow() {
  mainWindow = new BrowserWindow({
    show: false,
    icon: path.join(process.env.VITE_PUBLIC, "electron-vite.svg"),
    webPreferences: {
      preload: path.join(__dirname$1, "preload.mjs")
    }
  });
  mainWindow.webContents.on("did-finish-load", () => {
    mainWindow == null ? void 0 : mainWindow.webContents.send("main-process-message", (/* @__PURE__ */ new Date()).toLocaleString());
  });
  loadRenderer(mainWindow, "main");
}
function createChildWindow(title) {
  const key = title;
  const existingWindow = childWindows.get(key);
  if (existingWindow && !existingWindow.isDestroyed()) {
    existingWindow.focus();
    return;
  }
  const workArea = screen.getPrimaryDisplay().workArea;
  const maxColumns = Math.max(
    1,
    Math.floor((workArea.width - CHILD_WINDOW_BASE_X - CHILD_WINDOW_WIDTH) / CHILD_WINDOW_OFFSET_X) + 1
  );
  const maxRows = Math.max(
    1,
    Math.floor((workArea.height - CHILD_WINDOW_BASE_Y - CHILD_WINDOW_HEIGHT) / CHILD_WINDOW_OFFSET_Y) + 1
  );
  const totalSlots = maxColumns * maxRows;
  const slot = nextChildWindowSlot % totalSlots;
  nextChildWindowSlot += 1;
  const column = slot % maxColumns;
  const row = Math.floor(slot / maxColumns);
  const position = {
    x: workArea.x + CHILD_WINDOW_BASE_X + column * CHILD_WINDOW_OFFSET_X,
    y: workArea.y + CHILD_WINDOW_BASE_Y + row * CHILD_WINDOW_OFFSET_Y
  };
  const childWindow = new BrowserWindow({
    title,
    width: CHILD_WINDOW_WIDTH,
    height: CHILD_WINDOW_HEIGHT,
    ...position,
    icon: path.join(process.env.VITE_PUBLIC, "electron-vite.svg"),
    webPreferences: {
      preload: path.join(__dirname$1, "preload.mjs")
    }
  });
  childWindows.set(key, childWindow);
  childWindow.on("closed", () => {
    childWindows.delete(key);
  });
  loadRenderer(childWindow, "child", title);
}
function closeChildWindow(title) {
  const key = title;
  const existingWindow = childWindows.get(key);
  if (!existingWindow || existingWindow.isDestroyed()) {
    childWindows.delete(key);
    return;
  }
  childWindows.delete(key);
  existingWindow.close();
}
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
    mainWindow = null;
  }
});
app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createMainWindow();
  }
});
ipcMain.on("window:create", (_event, payload) => {
  if (!(payload == null ? void 0 : payload.title)) {
    return;
  }
  createChildWindow(payload.title);
});
ipcMain.on("window:close", (_event, title) => {
  if (!title) {
    return;
  }
  closeChildWindow(title);
});
ipcMain.on("window:sync", (_event, activeTitles) => {
  const titleSet = new Set(activeTitles.filter(Boolean));
  for (const title of Array.from(childWindows.keys())) {
    if (!titleSet.has(title)) {
      closeChildWindow(title);
    }
  }
});
app.whenReady().then(() => createMainWindow());
export {
  MAIN_DIST,
  RENDERER_DIST,
  VITE_DEV_SERVER_URL
};
