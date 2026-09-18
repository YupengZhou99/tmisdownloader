// Deadlines apply to protocol calls too: a polling loop alone cannot bound an
// evaluate/executeJavaScript promise after a renderer has stopped responding.
const fs = require('node:fs');
const path = require('node:path');

async function withDeadline(promise, milliseconds, label) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} exceeded ${milliseconds} ms`)), milliseconds);
    })]);
  } finally { clearTimeout(timer); }
}

function captureProcessOutput(application, artifacts, name) {
  const file = path.join(artifacts, name + '-electron.log');
  let tail = '';
  const append = chunk => {
    tail = (tail + chunk.toString()).slice(-1024 * 1024);
    fs.writeFileSync(file, tail);
  };
  application.process().stdout?.on('data', append);
  application.process().stderr?.on('data', append);
}

function collectDiagnostics(temporary, artifacts, name) {
  // Only this test's fresh private state, never a user's real workspace.
  const source = path.join(temporary, 'state', 'diagnostics');
  if (!fs.existsSync(source)) return;
  const destination = path.join(artifacts, name + '-diagnostics');
  fs.mkdirSync(destination, { recursive: true });
  for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
    if (entry.isFile() && /^stability\.jsonl(?:\.[1-4])?$/.test(entry.name))
      fs.copyFileSync(path.join(source, entry.name), path.join(destination, entry.name));
  }
}

async function closeApplication(application) {
  if (!application || application.process().exitCode !== null || application.process().signalCode !== null) return;
  try {
    await withDeadline(application.evaluate(({ dialog }) => {
      dialog.showMessageBox = async () => ({ response: 2 });
    }), 5000, 'prepare app shutdown');
    await withDeadline(application.close(), 30000, 'app shutdown');
  } catch (error) {
    // The ChildProcess belongs to this isolated launch; no global Electron or
    // Chromium process matching and no access to production browser profiles.
    application.process().kill('SIGKILL');
    throw error;
  }
}

module.exports = { withDeadline, captureProcessOutput, collectDiagnostics, closeApplication };
