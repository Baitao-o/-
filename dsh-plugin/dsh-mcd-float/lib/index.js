/**
 * dsh-mcd-float —— 服务端 Host 插件
 *
 * 职责只有两件：
 *   1. 提供浏览器端脚本路由 /dsh-mcd-float/float.js
 *   2. 把这段脚本注入到 DSH 页面（web 形态用 tapIndex，桌面形态用 webserver/index-inject）
 *
 * 注入方式的取舍（照抄 dsh-whale-widget 踩坑结论）：
 *   - 桌面端（Electron）只认 `webserver/index-inject` 推的**结构化行**；
 *   - 该行若用 kind:'script-src'，加载失败会 reject 掉 __DSH_BOOT_READY__，直接让应用起不来；
 *   - 所以用 kind:'script' 内联行，由注入代码自己建 <script src> 并**吞掉 onerror**——
 *     路由在就正常加载，路由不在就静默失败，宿主永远不会因为本插件起不来。
 */
import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PKG_ROOT = path.dirname(HERE)
const FLOAT_JS_CANDIDATES = [
  path.join(PKG_ROOT, 'assets', 'float.js'),
  path.join(HERE, 'float.js'),
]

// 按 mtime 缓存：只在文件改动时重读。
// 这样开发期改 assets/float.js 只需要**刷新页面**，不用重启桌面 App。
let floatJsCache = null
let floatJsMtime = 0

function loadFloatJs () {
  for (const p of FLOAT_JS_CANDIDATES) {
    try {
      const stat = fs.statSync(p)
      const mtime = stat.mtimeMs
      if (floatJsCache !== null && mtime === floatJsMtime) return floatJsCache
      floatJsCache = fs.readFileSync(p, 'utf8')
      floatJsMtime = mtime
      return floatJsCache
    } catch (err) { /* 继续找下一个 */ }
  }
  return floatJsCache || ''
}


// ---------------- 麦当劳服务的探测与拉起 ----------------

const MCD_PORTS = [8808, 8809, 8810, 8811, 8812]

// 仓库位置解析顺序：
//   ① 环境变量 MCD_REPO_ROOT（自定义路径）
//   ② skill_boot.py 启动时写到 ~/.maimen-budget-meal/repo.json 的注册信息
//      —— 这样**别人 clone 到任何目录**都能被本插件找到，不需要手改代码
//   ③ 作者开发机的默认位置（仅兜底）
function resolveRepoRoot () {
  const looksLikeRepo = (dir) => {
    try { return !!dir && fs.existsSync(path.join(dir, 'scripts', 'skill_boot.py')) } catch (err) { return false }
  }

  const fromEnv = process.env.MCD_REPO_ROOT
  if (looksLikeRepo(fromEnv)) return fromEnv

  try {
    const reg = path.join(os.homedir(), '.maimen-budget-meal', 'repo.json')
    const data = JSON.parse(fs.readFileSync(reg, 'utf8'))
    if (data && typeof data.root === 'string' && looksLikeRepo(data.root)) return data.root
  } catch (err) { /* 还没注册过 */ }

  return 'D:\\桌面\\WaytoAGI-Dine'
}
const SERVICE_NAME = 'maimen-budget-meal'

function probePort (port) {
  return new Promise((resolve) => {
    let done = false
    const finish = (v) => { if (!done) { done = true; resolve(v) } }
    try {
      const req = http.get({ host: '127.0.0.1', port, path: '/api/health', timeout: 700 }, (res) => {
        let body = ''
        res.setEncoding('utf8')
        res.on('data', (c) => { body += c })
        res.on('end', () => {
          try {
            const data = JSON.parse(body)
            finish(data && data.service === SERVICE_NAME ? port : null)
          } catch (err) { finish(null) }
        })
      })
      req.on('error', () => finish(null))
      req.on('timeout', () => { try { req.destroy() } catch (err) {} finish(null) })
    } catch (err) { finish(null) }
  })
}

async function findServicePort () {
  for (const port of MCD_PORTS) {
    const hit = await probePort(port)
    if (hit) return hit
  }
  return null
}

// 拉起服务：直接执行仓库里的 scripts/skill_boot.py（它自己会等就绪）
function spawnBoot () {
  const repo = resolveRepoRoot()
  const script = path.join(repo, 'scripts', 'skill_boot.py')
  if (!fs.existsSync(script)) {
    throw new Error(
      '找不到点餐仓库。请先手动在仓库目录跑一次 python scripts/skill_boot.py（它会登记位置），' +
      '或设置环境变量 MCD_REPO_ROOT 指向仓库根目录。当前尝试过：' + repo)
  }
  const candidates = process.platform === 'win32' ? ['python', 'py'] : ['python3', 'python']
  let child = null
  let lastErr = null
  for (const cmd of candidates) {
    try {
      child = spawn(cmd, [script, '--json', '--no-browser'], {
        cwd: repo,
        detached: true,
        stdio: 'ignore',
        windowsHide: true,
      })
      break
    } catch (err) { lastErr = err }
  }
  if (!child) throw new Error('无法启动 python：' + String((lastErr && lastErr.message) || lastErr))
  child.unref()
  return true
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const ROUTE = '/dsh-mcd-float/float.js'

// 内联注入行：自己建 script，自己吞错。
const INJECT_TEXT =
  '(function(){try{var d=document.body||document.head||document.documentElement;' +
  'if(!d)return;var s=document.createElement("script");' +
  's.src="' + ROUTE + '";s.onerror=function(){};' +
  's.setAttribute("data-dsh-mcd-float","1");d.appendChild(s)}catch(e){}})()'

export default {
  name: 'mcd-float',

  apply (root) {
    const disposers = []
    root.effect(() => () => {
      for (const d of disposers) { try { d() } catch (err) { /* 忽略清理异常 */ } }
    })

    // ---------- ① 桌面端（Electron）注入：必须在 apply 里同步注册，不能等任何服务 ----------
    disposers.push(root.on('webserver/index-inject', (table) => {
      try {
        if (!Array.isArray(table)) return
        for (const row of table) {
          if (!row) continue
          if (row.kind === 'script' && typeof row.text === 'string' && row.text.indexOf(ROUTE) >= 0) return
          if (row.kind === 'script-src' && row.src === ROUTE) return
        }
        table.push({ kind: 'script', placement: 'body', text: INJECT_TEXT })
      } catch (err) { /* 注入失败不应影响宿主 */ }
    }))

    // ---------- ② Web 形态：等 webServer 就绪后注册路由 + tapIndex ----------
    root.inject(['webServer'], (ctx) => {
      try {
        disposers.push(ctx.webServer.register({
          path: ROUTE,
          handler: (req, res) => {
            res.writeHead(200, {
              'Content-Type': 'application/javascript; charset=utf-8',
              'Cache-Control': 'no-store',
              'Access-Control-Allow-Origin': '*',
            })
            res.end(loadFloatJs())
          },
        }))
      } catch (err) {
        try { console.warn('[dsh-mcd-float] 注册路由失败：' + String((err && err.message) || err)) } catch (e2) {}
      }


      // ---- 拉起接口：服务没起时由这里把它启动起来 ----
      try {
        disposers.push(ctx.webServer.register({
          path: '/dsh-mcd-float/boot',
          handler: async (req, res) => {
            const send = (code, obj) => {
              try {
                res.writeHead(code, {
                  'Content-Type': 'application/json; charset=utf-8',
                  'Cache-Control': 'no-store',
                  'Access-Control-Allow-Origin': '*',
                })
                res.end(JSON.stringify(obj))
              } catch (err) { /* 连接已断 */ }
            }
            try {
              let port = await findServicePort()
              let started = false
              if (!port) {
                spawnBoot()
                started = true
                const deadline = Date.now() + 32000
                while (Date.now() < deadline) {
                  await sleep(700)
                  port = await findServicePort()
                  if (port) break
                }
              }
              if (!port) return send(503, { ok: false, error: '服务启动超时（32 秒未就绪），请手动运行 python scripts/skill_boot.py' })
              send(200, { ok: true, started, port, url: 'http://127.0.0.1:' + port + '/' })
            } catch (err) {
              send(500, { ok: false, error: String((err && err.message) || err) })
            }
          },
        }))
      } catch (err) {
        try { console.warn('[dsh-mcd-float] 注册 boot 路由失败：' + String((err && err.message) || err)) } catch (e2) {}
      }

      try {
        disposers.push(ctx.webServer.tapIndex((html) => {
          if (!html || html.indexOf(ROUTE) >= 0) return html
          const tag = '<script defer src="' + ROUTE + '"></script>'
          if (html.indexOf('</body>') !== -1) return html.replace('</body>', tag + '</body>')
          return html + tag
        }))
      } catch (err) {
        try { console.warn('[dsh-mcd-float] tapIndex 注入失败：' + String((err && err.message) || err)) } catch (e2) {}
      }
    })
  },
}
