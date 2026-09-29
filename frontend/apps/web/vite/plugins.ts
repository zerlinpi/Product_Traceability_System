import type { PluginOption } from 'vite'
import { ComponentsAutoImports as FantasticAdminComponentsAutoImports, ComponentsResolver as FantasticAdminComponentsResolver, ComponentsType as FantasticAdminComponentsType } from '@fantastic-admin/components/resolver'
import { ComposablesAutoImports as FantasticAdminComposablesAutoImports } from '@fantastic-admin/composables/resolver'
import vue from '@vitejs/plugin-vue'
import vueJsx from '@vitejs/plugin-vue-jsx'
import Unocss from 'unocss/vite'
import autoImport from 'unplugin-auto-import/vite'
import components from 'unplugin-vue-components/vite'
import { envParse } from 'vite-plugin-env-parse'

/**
 * The Flask server sends a strict Content-Security-Policy:
 *
 *   default-src 'self'; img-src 'self' data:; style-src 'self';
 *   script-src 'self'; connect-src 'self'
 *
 * so the built index.html must not carry any inline <script>, inline <style>,
 * style="" attribute or absolute third-party URL. Plugins that inject such
 * markup (app-loading, legacy, devtools, fake-server) are deliberately not
 * used. This guard fails the build instead of shipping a page the browser
 * would silently refuse to run.
 */
function cspGuard(): PluginOption {
  return {
    name: 'pts-csp-guard',
    apply: 'build',
    enforce: 'post',
    transformIndexHtml: {
      order: 'post',
      handler(html) {
        const problems: string[] = []
        for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
          if (!/\bsrc=/i.test(match[1]) || match[2].trim() !== '') {
            problems.push('inline <script>')
          }
        }
        if (/<style\b/i.test(html)) {
          problems.push('inline <style>')
        }
        if (/\sstyle\s*=/i.test(html)) {
          problems.push('style="" attribute')
        }
        if (/https?:\/\//i.test(html)) {
          problems.push('absolute http(s) URL')
        }
        if (problems.length > 0) {
          throw new Error(`[pts-csp-guard] index.html violates the server CSP: ${[...new Set(problems)].join(', ')}`)
        }
        return html
      },
    },
  }
}

export default function createVitePlugins(_mode: string, _isBuild = false) {
  const vitePlugins: (PluginOption | PluginOption[])[] = [
    vue(),
    vueJsx(),

    // https://github.com/yue1123/vite-plugin-env-parse
    envParse({
      dtsPath: 'src/types/env.d.ts',
    }),

    // https://github.com/unplugin/unplugin-auto-import
    autoImport({
      imports: [
        'vue',
        'vue-router',
        'pinia',
        FantasticAdminComponentsAutoImports,
        FantasticAdminComposablesAutoImports,
      ],
      dts: './src/types/auto-imports.d.ts',
      dirs: [
        './src/store/modules/**/*',
        './src/composables/**/*',
      ],
    }),

    // https://github.com/unplugin/unplugin-vue-components
    components({
      globs: [
        'src/components/*/index.vue',
      ],
      dts: './src/types/components.d.ts',
      resolvers: [
        FantasticAdminComponentsResolver(),
      ],
      types: [
        FantasticAdminComponentsType,
      ],
    }),

    Unocss(),

    cspGuard(),
  ]
  return vitePlugins
}
