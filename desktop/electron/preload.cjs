const { contextBridge, ipcRenderer, webUtils } = require('electron');
contextBridge.exposeInMainWorld('tmis', {
  call: (command, data = {}) => ipcRenderer.invoke('tmis:call', command, data),
  files: () => ipcRenderer.invoke('tmis:files'),
  directory: () => ipcRenderer.invoke('tmis:directory'),
  browser: () => ipcRenderer.invoke('tmis:browser'),
  drop: files => ipcRenderer.invoke('tmis:drop', files.map(file => webUtils.getPathForFile(file))),
  window: (action, value) => ipcRenderer.invoke('tmis:window', action, value),
  open: path => ipcRenderer.invoke('tmis:open', path),
  subscribe: listener => {
    const handler = (_event, data) => listener(data);
    ipcRenderer.on('tmis:event', handler);
    return () => ipcRenderer.removeListener('tmis:event', handler);
  }
});
