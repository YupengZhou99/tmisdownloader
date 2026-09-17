const esbuild = require('esbuild');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
async function build() {
  fs.mkdirSync(path.join(root, 'dist'), { recursive: true });
  fs.copyFileSync(path.join(root, 'index.html'), path.join(root, 'dist/index.html'));
  const licenses = path.join(root, 'dist/licenses');
  fs.mkdirSync(licenses, { recursive: true });
  for (const name of ['react', 'react-dom', 'lucide-react']) {
    fs.copyFileSync(path.join(root, 'node_modules', name, 'LICENSE'), path.join(licenses, name + '.txt'));
  }
  const settings = { absWorkingDir: root, entryPoints: ['src/main.tsx'], bundle: true,
    outfile: 'dist/main.js', minify: true, target: ['chrome120'], jsx: 'automatic',
    define: { 'process.env.NODE_ENV': '"production"' }, logLevel: 'info' };
  if (process.argv.includes('--serve')) {
    const context = await esbuild.context(settings);
    await context.watch();
    await context.serve({ servedir: path.join(root, 'dist'), host: '127.0.0.1', port: 5173 });
  } else await esbuild.build(settings);
}
build().catch(error => { console.error(error); process.exitCode = 1; });
