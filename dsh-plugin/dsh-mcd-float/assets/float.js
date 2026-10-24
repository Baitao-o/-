/**
 * dsh-mcd-float —— 浏览器端悬浮球
 *
 * 行为：
 *   · 只在 DSH 主聊天界面挂载（检测到 composer 才碰 DOM，避免污染插件市场等 SPA 视图）
 *   · 右下角悬浮球，可拖动，位置记在 localStorage
 *   · 点一下 → 探测麦当劳服务（8808~8812），在线就打开它的网页界面
 *   · 新窗口被拦时，降级为应用内 iframe 弹层（同一套前端，仍然可用）
 *   · 左下角小圆点显示服务在线状态（绿=在线 / 灰=未启动）
 *
 * 样式一律用 CSSOM（el.style），不用 <style> 标签 —— 避免被宿主 CSP 的 style-src 拦掉。
 */
(function () {
  if (window.__dshMcdFloat) return
  window.__dshMcdFloat = true

  var NS = 'dsh-mcd-float'
  var PORTS = [8808, 8809, 8810, 8811, 8812]
  var POS_KEY = NS + '-pos'
  var SIZE = 58
  var DRAG_THRESHOLD = 5
  var started = false

  // ---------- 只在主聊天界面挂载 ----------
  function isChatRoot (r) {
    if (!r || !r.querySelector) return false
    return !!(
      r.querySelector('textarea') ||
      r.querySelector('[contenteditable="true"]') ||
      r.querySelector('[data-composer-input]') ||
      r.querySelector('[data-composer-seat],[data-composer-card]') ||
      r.querySelector('[role="textbox"][aria-multiline="true"]')
    )
  }

  function css (el, obj) {
    for (var k in obj) { if (Object.prototype.hasOwnProperty.call(obj, k)) el.style[k] = obj[k] }
  }

  // ---------- 状态 ----------
  var state = { port: null, online: false, timer: null }

  function probeOnce () {
    var i = 0
    function next () {
      if (i >= PORTS.length) { state.port = null; return Promise.resolve(null) }
      var port = PORTS[i++]
      return fetch('http://127.0.0.1:' + port + '/api/health', { cache: 'no-store' })
        .then(function (res) {
          if (!res.ok) throw new Error('bad status')
          return res.json()
        })
        .then(function (data) {
          if (data && data.service === 'maimen-budget-meal') {
            state.port = port
            return data
          }
          return next()
        })
        .catch(function () { return next() })
    }
    return next()
  }

  function refreshStatus () {
    return probeOnce().then(function (info) {
      state.online = !!info
      paintDot()
      return info
    }).catch(function () { state.online = false; paintDot(); return null })
  }

  // ---------- DOM ----------
  var ballEl = null
  var dotEl = null
  var tipEl = null

  function paintDot () {
    if (!dotEl) return
    dotEl.style.background = state.online ? '#22c55e' : '#9ca3af'
    dotEl.title = state.online ? '麦当劳服务在线' : '麦当劳服务未启动'
  }

  function buildBall () {
    if (ballEl) return ballEl

    var wrap = document.createElement('div')
    wrap.id = NS
    css(wrap, {
      position: 'fixed',
      right: '24px',
      bottom: '148px',
      width: SIZE + 'px',
      height: SIZE + 'px',
      zIndex: '2147483000',
      cursor: 'grab',
      userSelect: 'none',
      touchAction: 'none',
      transition: 'transform .16s ease, box-shadow .16s ease',
    })

    // 球体
    var ball = document.createElement('div')
    css(ball, {
      width: '100%',
      height: '100%',
      borderRadius: '50%',
      background: 'linear-gradient(145deg, #DA291C, #b31f14)',
      boxShadow: '0 6px 18px rgba(218,41,28,.42), inset 0 -3px 8px rgba(0,0,0,.18), inset 0 3px 8px rgba(255,255,255,.22)',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      fontSize: '27px',
      lineHeight: '1',
      boxSizing: 'border-box',
      border: '2px solid rgba(255,255,255,.85)',
    })
    ball.textContent = '🍔'

    // 在线状态点
    dotEl = document.createElement('span')
    css(dotEl, {
      position: 'absolute',
      right: '-2px',
      bottom: '-2px',
      width: '14px',
      height: '14px',
      borderRadius: '50%',
      background: '#9ca3af',
      border: '2px solid #fff',
      boxShadow: '0 1px 3px rgba(0,0,0,.2)',
      boxSizing: 'border-box',
    })

    // 提示文字
    tipEl = document.createElement('div')
    css(tipEl, {
      position: 'absolute',
      right: (SIZE + 10) + 'px',
      top: '50%',
      transform: 'translateY(-50%)',
      whiteSpace: 'nowrap',
      background: 'rgba(26,26,26,.92)',
      color: '#fff',
      fontSize: '12px',
      fontWeight: '600',
      padding: '6px 12px',
      borderRadius: '8px',
      opacity: '0',
      pointerEvents: 'none',
      transition: 'opacity .16s ease',
    })
    tipEl.textContent = '麦门省钱搭子'

    wrap.appendChild(ball)
    wrap.appendChild(dotEl)
    wrap.appendChild(tipEl)

    // hover 反馈
    wrap.addEventListener('mouseenter', function () {
      wrap.style.transform = 'scale(1.08)'
      if (tipEl) tipEl.style.opacity = '1'
    })
    wrap.addEventListener('mouseleave', function () {
      wrap.style.transform = 'scale(1)'
      if (tipEl) tipEl.style.opacity = '0'
    })

    attachDragAndClick(wrap)
    document.body.appendChild(wrap)
    ballEl = wrap
    restorePos()
    return wrap
  }

  // ---------- 拖动 + 点击 ----------
  function attachDragAndClick (wrap) {
    var dragging = false
    var moved = false
    var startX = 0
    var startY = 0
    var originLeft = 0
    var originTop = 0

    wrap.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return
      var rect = wrap.getBoundingClientRect()
      dragging = true
      moved = false
      startX = e.clientX
      startY = e.clientY
      originLeft = rect.left
      originTop = rect.top
      wrap.style.transition = 'none'
      wrap.style.cursor = 'grabbing'
      try { wrap.setPointerCapture(e.pointerId) } catch (err) { /* 老浏览器忽略 */ }
    })

    wrap.addEventListener('pointermove', function (e) {
      if (!dragging) return
      var dx = e.clientX - startX
      var dy = e.clientY - startY
      if (!moved && Math.abs(dx) + Math.abs(dy) > DRAG_THRESHOLD) moved = true
      if (!moved) return
      var left = Math.min(Math.max(4, originLeft + dx), window.innerWidth - SIZE - 4)
      var top = Math.min(Math.max(4, originTop + dy), window.innerHeight - SIZE - 4)
      wrap.style.left = left + 'px'
      wrap.style.top = top + 'px'
      wrap.style.right = 'auto'
      wrap.style.bottom = 'auto'
    })

    wrap.addEventListener('pointerup', function (e) {
      if (!dragging) return
      dragging = false
      wrap.style.transition = 'transform .16s ease, box-shadow .16s ease'
      wrap.style.cursor = 'grab'
      if (typeof wrap.releasePointerCapture === 'function') {
        try { wrap.releasePointerCapture(e.pointerId) } catch (err) { /* ignore */ }
      }
      if (moved) {
        savePos()
      } else {
        onBallClick()
      }
    })

    wrap.addEventListener('pointercancel', function () {
      dragging = false
      wrap.style.cursor = 'grab'
    })
  }

  function savePos () {
    try {
      var rect = ballEl.getBoundingClientRect()
      localStorage.setItem(POS_KEY, JSON.stringify({ left: rect.left, top: rect.top }))
    } catch (err) { /* 隐私模式下忽略 */ }
  }

  function restorePos () {
    try {
      var raw = localStorage.getItem(POS_KEY)
      if (!raw) return
      var pos = JSON.parse(raw)
      if (!pos || typeof pos.left !== 'number' || typeof pos.top !== 'number') return
      var left = Math.min(Math.max(4, pos.left), window.innerWidth - SIZE - 4)
      var top = Math.min(Math.max(4, pos.top), window.innerHeight - SIZE - 4)
      ballEl.style.left = left + 'px'
      ballEl.style.top = top + 'px'
      ballEl.style.right = 'auto'
      ballEl.style.bottom = 'auto'
    } catch (err) { /* ignore */ }
  }

  // ---------- 点击：打开麦当劳前端 ----------
  function pulse () {
    if (!ballEl) return
    ballEl.style.transform = 'scale(0.88)'
    setTimeout(function () { if (ballEl) ballEl.style.transform = 'scale(1)' }, 130)
  }

  // 调插件服务端：确保服务在跑（没跑就拉起），返回可用地址
  function ensureService () {
    // 快路径：本地探测（可能被 CSP 拦，失败就往下走）
    return probeOnce().then(function (info) {
      if (info && state.port) return 'http://127.0.0.1:' + state.port + '/'
      // 慢路径：同源接口（不受 connect-src 限制），服务没起时它会负责拉起
      return fetch('/dsh-mcd-float/boot', { method: 'POST' })
        .then(function (res) { return res.json() })
        .then(function (d) {
          if (d && d.ok) { state.port = d.port; return d.url }
          throw new Error((d && d.error) || '唤起失败')
        })
    }).catch(function (err) {
      lastBootError = (err && err.message) ? err.message : String(err)
      return null
    })
  }

  var lastBootError = ''

  function onBallClick () {
    pulse()

    // ★ 关键：**同步**先开一个空白窗口。
    // 浏览器要求 window.open 必须在用户手势的调用栈里；一旦先 await 探测/启动，
    // 手势上下文丢失，弹窗就会被拦。
    var win = null
    try { win = window.open('', '_blank') } catch (err) { win = null }
    if (win) {
      try {
        win.document.write(
          '<!doctype html><meta charset="utf-8"><title>正在唤起…</title>' +
          '<body style="font-family:-apple-system,\'PingFang SC\',sans-serif;' +
          'display:flex;align-items:center;justify-content:center;height:100vh;margin:0;' +
          'background:#fafafa;color:#666;font-size:15px">🍔 正在唤起麦门省钱搭子…</body>'
        )
      } catch (err) { /* 忽略 */ }
    } else {
      // 窗口都被拦了，先给兜底入口（下面还会再试一次）
      toast('窗口被拦截，正在准备兜底方式…')
    }

    ensureService().then(function (url) {
      if (!url) {
        if (win && !win.closed) { try { win.close() } catch (err) { /* ignore */ } }
        showFailTip(lastBootError)
        return
      }
      // 把已经开好的窗口导过去（跨源导航是允许的）
      if (win && !win.closed) {
        try {
          win.location.href = url
          refreshStatus()
          return
        } catch (err) { /* 落到兜底 */ }
      }
      // 窗口没了 / 被拦：再试一次 window.open，仍失败就出兜底浮层
      var opened = false
      try {
        var w2 = window.open(url, '_blank', 'noopener,noreferrer')
        opened = !!w2
      } catch (err) { opened = false }
      showFallbackTip(url, opened)
      refreshStatus()
    })
  }

  // 彻底失败时的提示
  function showFailTip (reason) {
    toast('麦当劳服务没能启动。\n' + (reason || '') +
      '\n可以手动在仓库运行：python scripts/skill_boot.py')
  }

  // 打开失败时的兜底小浮层
  var fbEl = null
  var fbTimer = null
  function showFallbackTip (url, opened) {
    if (fbEl) { try { fbEl.remove() } catch (err) { /* ignore */ } fbEl = null }
    if (fbTimer) { clearTimeout(fbTimer); fbTimer = null }

    var wrap = document.createElement('div')
    css(wrap, {
      position: 'fixed',
      right: '24px',
      bottom: '218px',
      zIndex: '2147483002',
      background: 'rgba(26,26,26,.95)',
      color: '#fff',
      padding: '10px 12px',
      borderRadius: '10px',
      boxShadow: '0 10px 28px rgba(0,0,0,.32)',
      fontFamily: 'inherit',
      maxWidth: '260px',
    })

    var line = document.createElement('div')
    css(line, { fontSize: '12px', lineHeight: '1.6', marginBottom: '8px' })
    line.textContent = opened
      ? '已在新的窗口打开麦门省钱搭子'
      : '没弹出来？宿主可能拦截了新窗口，用下面任一条：'
    wrap.appendChild(line)

    var row = document.createElement('div')
    css(row, { display: 'flex', gap: '6px', flexWrap: 'wrap' })

    function tiny (text, bg, color, border) {
      var b = document.createElement('button')
      b.textContent = text
      css(b, {
        border: border || 'none',
        background: bg,
        color: color,
        fontSize: '11.5px',
        fontWeight: '700',
        padding: '5px 9px',
        borderRadius: '7px',
        cursor: 'pointer',
        fontFamily: 'inherit',
        whiteSpace: 'nowrap',
      })
      return b
    }

    var btnInline = tiny('🖥 应用内打开', '#DA291C', '#fff')
    btnInline.addEventListener('click', function () {
      openPanel(url)
      if (fbEl) { try { fbEl.remove() } catch (err) { /* ignore */ } fbEl = null }
    })

    var btnCopy = tiny('⧉ 复制地址', '#fff', '#374151', '1.5px solid #555')
    btnCopy.addEventListener('click', function () {
      var ok = copyText(url)
      btnCopy.textContent = ok ? '✅ 已复制' : '复制失败'
      setTimeout(function () { btnCopy.textContent = '⧉ 复制地址' }, 1600)
    })

    var btnClose = tiny('✕', 'transparent', '#bbb')
    btnClose.addEventListener('click', function () {
      if (fbEl) { try { fbEl.remove() } catch (err) { /* ignore */ } fbEl = null }
    })

    row.appendChild(btnInline)
    row.appendChild(btnCopy)
    row.appendChild(btnClose)
    wrap.appendChild(row)

    document.body.appendChild(wrap)
    fbEl = wrap

    fbTimer = setTimeout(function () {
      if (fbEl) {
        fbEl.style.transition = 'opacity .3s ease'
        fbEl.style.opacity = '0'
        setTimeout(function () {
          if (fbEl) { try { fbEl.remove() } catch (err) { /* ignore */ } fbEl = null }
        }, 320)
      }
    }, 3400)
  }

  function copyText (text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text)
        return true
      }
    } catch (err) { /* ignore */ }
    try {
      var ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      ta.remove()
      return true
    } catch (err) { return false }
  }

  // 应用内面板：最可靠的一条路（不依赖宿主对 window.open 的策略）
  function openPanel (url) {
    var existing = document.getElementById(NS + '-overlay')
    if (existing) {
      try { existing.remove() } catch (err) { /* ignore */ }
    }

    var mask = document.createElement('div')
    mask.id = NS + '-overlay'
    css(mask, {
      position: 'fixed',
      inset: '0',
      zIndex: '2147483001',
      background: 'rgba(0,0,0,.55)',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '2vh 2vw',
      boxSizing: 'border-box',
    })

    var panel = document.createElement('div')
    css(panel, {
      width: '100%',
      maxWidth: '1120px',
      height: '92vh',
      background: '#fff',
      borderRadius: '14px',
      overflow: 'hidden',
      boxShadow: '0 18px 60px rgba(0,0,0,.4)',
      display: 'flex',
      flexDirection: 'column',
    })

    // ---- 顶部栏 ----
    var bar = document.createElement('div')
    css(bar, {
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      gap: '10px',
      padding: '9px 14px',
      borderBottom: '1px solid #ececf0',
      background: '#fafafa',
      flexShrink: '0',
    })

    var title = document.createElement('span')
    css(title, { fontSize: '13px', fontWeight: '700', color: '#1a1a1a', whiteSpace: 'nowrap' })
    title.textContent = '🍔 麦门省钱搭子'

    var actions = document.createElement('div')
    css(actions, { display: 'flex', alignItems: 'center', gap: '8px' })

    function mkBtn (text, bg, color, border) {
      var b = document.createElement('button')
      b.textContent = text
      css(b, {
        border: border || 'none',
        background: bg,
        color: color,
        fontSize: '12px',
        fontWeight: '700',
        padding: '6px 12px',
        borderRadius: '8px',
        cursor: 'pointer',
        fontFamily: 'inherit',
        whiteSpace: 'nowrap',
      })
      return b
    }

    // 新窗口（原生 a 标签，交给宿主决定行为）
    var newWin = document.createElement('a')
    newWin.href = url
    newWin.target = '_blank'
    newWin.rel = 'noopener noreferrer'
    newWin.textContent = '↗ 新窗口'
    css(newWin, {
      border: '1.5px solid #e5e7eb',
      background: '#fff',
      color: '#374151',
      fontSize: '12px',
      fontWeight: '700',
      padding: '5px 11px',
      borderRadius: '8px',
      textDecoration: 'none',
      whiteSpace: 'nowrap',
    })

    var copyBtn = mkBtn('⧉ 复制地址', '#fff', '#374151', '1.5px solid #e5e7eb')
    copyBtn.addEventListener('click', function () {
      var ok = copyText(url)
      copyBtn.textContent = ok ? '✅ 已复制' : '复制失败'
      setTimeout(function () { copyBtn.textContent = '⧉ 复制地址' }, 1600)
    })

    var closeBtn = mkBtn('关闭 ✕', '#DA291C', '#fff')

    actions.appendChild(newWin)
    actions.appendChild(copyBtn)
    actions.appendChild(closeBtn)
    bar.appendChild(title)
    bar.appendChild(actions)

    // ---- 状态提示条 ----
    var statusTip = document.createElement('div')
    statusTip.id = NS + '-status-tip'
    css(statusTip, {
      display: 'none',
      fontSize: '12px',
      lineHeight: '1.6',
      padding: '8px 14px',
      background: '#eefbf2',
      color: '#0f7b46',
      borderBottom: '1px solid #ececf0',
      flexShrink: '0',
    })

    // ---- iframe ----
    var frame = document.createElement('iframe')
    frame.src = url
    frame.setAttribute('allow', 'clipboard-write')
    css(frame, { flex: '1', width: '100%', border: 'none', display: 'block', background: '#fff' })

    panel.appendChild(bar)
    panel.appendChild(statusTip)
    panel.appendChild(frame)
    mask.appendChild(panel)
    document.body.appendChild(mask)

    function destroy () {
      try { mask.remove() } catch (err) { /* ignore */ }
      document.removeEventListener('keydown', onKey)
    }
    function onKey (e) { if (e.key === 'Escape') destroy() }
    document.addEventListener('keydown', onKey)
    closeBtn.addEventListener('click', destroy)
    mask.addEventListener('click', function (e) { if (e.target === mask) destroy() })
  }

  // ---------- 轻量 toast（同样不用 <style>） ----------
  var toastEl = null
  var toastTimer = null
  function toast (msg) {
    if (!toastEl) {
      toastEl = document.createElement('div')
      css(toastEl, {
        position: 'fixed',
        right: '24px',
        bottom: '220px',
        zIndex: '2147483002',
        maxWidth: '300px',
        whiteSpace: 'pre-line',
        background: 'rgba(26,26,26,.94)',
        color: '#fff',
        fontSize: '12.5px',
        lineHeight: '1.6',
        padding: '10px 14px',
        borderRadius: '10px',
        boxShadow: '0 8px 24px rgba(0,0,0,.28)',
        opacity: '0',
        transform: 'translateY(6px)',
        transition: 'opacity .2s ease, transform .2s ease',
        pointerEvents: 'none',
      })
      document.body.appendChild(toastEl)
    }
    toastEl.textContent = msg
    toastEl.style.opacity = '1'
    toastEl.style.transform = 'translateY(0)'
    if (toastTimer) clearTimeout(toastTimer)
    toastTimer = setTimeout(function () {
      if (toastEl) { toastEl.style.opacity = '0'; toastEl.style.transform = 'translateY(6px)' }
    }, 3600)
  }

  // ---------- 启动 ----------
  function init () {
    if (document.getElementById(NS)) return
    buildBall()
    refreshStatus()
    if (state.timer) clearInterval(state.timer)
    state.timer = setInterval(refreshStatus, 10000)
    window.addEventListener('resize', function () {
      if (!ballEl) return
      var rect = ballEl.getBoundingClientRect()
      if (rect.right > window.innerWidth || rect.bottom > window.innerHeight) {
        ballEl.style.left = ''
        ballEl.style.top = ''
        ballEl.style.right = '24px'
        ballEl.style.bottom = '148px'
      }
    })
  }

  function startOnce () {
    if (started) return
    started = true
    try { init() } catch (err) { /* 永不影响宿主 */ }
  }

  var lastCheck = 0
  function tryStart (force) {
    if (started) return true
    var now = Date.now()
    if (!force && now - lastCheck < 200) return false
    lastCheck = now
    try {
      if (isChatRoot(document.getElementById('root'))) { startOnce(); return true }
    } catch (err) { /* ignore */ }
    return false
  }

  try {
    if (!tryStart(true)) {
      var observer = new MutationObserver(function () { tryStart(false) })
      observer.observe(document.documentElement, { childList: true, subtree: true })
      // 保险：页面路由切换后再试几次
      var tries = 0
      var poll = setInterval(function () {
        tries += 1
        if (tryStart(false) || tries > 40) {
          clearInterval(poll)
          try { observer.disconnect() } catch (err) { /* ignore */ }
        }
      }, 500)
    }
  } catch (err) { /* ignore */ }
})()
