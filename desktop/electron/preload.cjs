const { contextBridge, ipcRenderer, webUtils } = require('electron');
const listeners = new Set();
ipcRenderer.on('tmis:event', (_event, data) => {
  const items = data.event === 'bundle' ? data.items : [data];
  try { for (const item of items) for (const listener of listeners) { try { listener(item); } catch (error) { console.error('UI event failed', error); } } }
  finally { if (data.event === 'bundle') ipcRenderer.send('tmis:ack', data.seq); }
});
contextBridge.exposeInMainWorld('tmis', {
  call: (command, data = {}, workspaceId) => ipcRenderer.invoke('tmis:call', command, data, workspaceId),
  workspaces: (action, data = {}) => ipcRenderer.invoke('tmis:workspaces', action, data),
  files: workspaceId => ipcRenderer.invoke('tmis:files', workspaceId),
  directory: workspaceId => ipcRenderer.invoke('tmis:directory', workspaceId),
  browser: () => ipcRenderer.invoke('tmis:browser'),
  drop: (files, workspaceId) => ipcRenderer.invoke('tmis:drop', files.map(file => webUtils.getPathForFile(file)), workspaceId),
  window: (action, value) => ipcRenderer.invoke('tmis:window', action, value),
  open: (path, workspaceId) => ipcRenderer.invoke('tmis:open', path, workspaceId),
  subscribe: listener => {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }
});
