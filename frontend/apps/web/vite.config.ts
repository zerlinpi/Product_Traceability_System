import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { execFileSync } from 'node:child_process'
import { defineConfig, loadEnv } from 'vite'
import { parseLoadedEnv } from 'vite-plugin-env-parse'
import pkg from './package.json' with { type: 'json' }
import createVitePlugins from './vite/plugins.ts'

// The build stamps itself with a time, shown in 系统信息 in the UI. It used to
// be `dayjs()`, i.e. now — which made every build produce a different bundle.
// Two builds two seconds apart disagreed, and so did the same source built on
// Windows and on Linux. The consequence was concrete: static/dist is committed
// (production servers have no Node), so the committed bundle could never be
// compared against a rebuild, and "somebody edited the front end and forgot to
// rebuild" had no way of being detected.
//
// Derive it from something stable instead:
//
//   SOURCE_DATE_EPOCH  the reproducible-builds convention, honoured when set
//   git commit time    otherwise — stable for a given revision
//   epoch 0            when neither exists (a source tarball with no .git)
//
// UTC, so two machines in different timezones agree. The value still means
// something: it is the revision the bundle was built from.
function resolveBuildTime(): string {
  const fromEnv = process.env.SOURCE_DATE_EPOCH
  const fromGit = (() => {
    try {
      return execFileSync('git', ['log', '-1', '--format=%ct'], {
        cwd: import.meta.dirname,
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
      }).trim()
    }
    catch {
      return ''
    }
  })()

  const seconds = [fromEnv, fromGit].find(value => value && /^\d+$/.test(value))
  return new Date(Number(seconds ?? 0) * 1000)
    .toISOString()
    .slice(0, 19)
    .replace('T', ' ')
}

// The Flask app serves everything under /static from the repository's static/
// folder, and the SPA entry (index.html) from static/dist. The build therefore
// writes straight into <repo>/static/dist so a plain `git pull` + restart ships
// the new UI; production servers never need Node.js.
const OUTPUT_DIR = path.resolve(import.meta.dirname, '../../../static/dist')
const PUBLIC_BASE = '/static/dist/'

export default defineConfig(({ mode, command }) => {
  const env = parseLoadedEnv(loadEnv(mode, process.cwd()))
  // 全局 scss 资源
  const scssResources: string[] = []
  fs.readdirSync('src/assets/styles/resources').forEach((dirname) => {
    if (fs.statSync(`src/assets/styles/resources/${dirname}`).isFile()) {
      scssResources.push(`@use "/src/assets/styles/resources/${dirname}" as *;`)
    }
  })
  return {
    base: command === 'build' ? PUBLIC_BASE : '/',
    // 开发服务器：接口请求代理到本地 Flask 服务（start.bat 默认端口 5080）
    server: {
      open: false,
      host: true,
      port: 9000,
      proxy: {
        '/api': {
          target: env.VITE_DEV_API_TARGET || 'http://127.0.0.1:5080',
          changeOrigin: false,
        },
      },
    },
    build: {
      outDir: OUTPUT_DIR,
      emptyOutDir: true,
      sourcemap: false,
      // Chromium/Edge on the shop-floor PCs; no legacy (inline-script) bundles.
      target: 'es2022',
      chunkSizeWarningLimit: 2048,
      // Never inline assets as data: URIs into JS — keep the CSP surface boring.
      assetsInlineLimit: 0,
    },
    define: {
      __SYSTEM_INFO__: JSON.stringify({
        pkg: {
          version: pkg.version ?? '',
          dependencies: pkg.dependencies,
          devDependencies: pkg.devDependencies,
        },
        lastBuildTime: resolveBuildTime(),
      }),
    },
    plugins: createVitePlugins(mode, command === 'build'),
    optimizeDeps: {
      exclude: [
        '@fantastic-admin/components',
        '@fantastic-admin/composables',
      ],
    },
    resolve: {
      alias: {
        '@': path.resolve(import.meta.dirname, 'src'),
        '#': path.resolve(import.meta.dirname, 'src/types'),
      },
    },
    css: {
      preprocessorOptions: {
        scss: {
          additionalData: scssResources.join(''),
        },
      },
    },
  }
})
