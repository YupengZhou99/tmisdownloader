const { contextBridge, ipcRenderer, webUtils } = require('electron');
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
    const handler = (_event, data) => listener(data);
    ipcRenderer.on('tmis:event', handler);
    return () => ipcRenderer.removeListener('tmis:event', handler);
  }
});
