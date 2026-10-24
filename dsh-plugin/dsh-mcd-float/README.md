# dsh-mcd-float（配套工具 · 非参赛主体）

> 这是「麦门省钱搭子」的**配套 DSH 插件**，不属于参赛作品的必要组成部分。
> 作用是：在 DSH 界面上放一个悬浮球，点一下直接打开点餐前端。
> 放在仓库里是作为「与 DSH 客户端深度集成」的工程示例，**删掉它不影响作品运行**。

## 效果

- 悬浮在 DSH 对话界面右侧，**可拖动**，位置记在 localStorage
- 左下角小圆点显示服务状态：🟢 在线 / ⚪ 未启动
- **单击**：
  1. 同步开一个窗口（保住浏览器"用户手势"，否则后面会被拦）
  2. 后台探测 `8808~8812`；没在跑就**自动拉起**（调本插件服务端 spawn `skill_boot.py`）
  3. 就绪后把窗口导航到点餐前端
  4. 窗口被拦时，右下角浮层给两条后路：**应用内打开** / **复制地址**
- 只在 DSH **主聊天界面**挂载，插件市场等 SPA 视图不碰 DOM

## 它怎么找到你的点餐仓库

插件装在 DSH 的 profile 里，并不知道你把仓库 clone 到哪去了。解析顺序：

| 优先级 | 来源 |
|---|---|
| ① | 环境变量 `MCD_REPO_ROOT`（自定义路径） |
| ② | `~/.maimen-budget-meal/repo.json` —— **只要你在仓库里跑过一次 `scripts/skill_boot.py`，它就会自动登记** |
| ③ | 作者开发机的默认路径（仅兜底） |

所以正常使用顺序是：**先在仓库里跑一次 `python scripts/skill_boot.py`**（完成位置登记），
之后再点悬浮球，就能自动拉起 + 打开了。

## 安装

`desktop` profile 由桌面 App 独占管理，`dsh plugin --profile desktop add` 会被 CLI 拒绝，
所以手工两步：

```bash
# 1) 复制到 profile 的 node_modules
cp -r dsh-plugin/dsh-mcd-float "C:/Users/<你>/.dsh/profiles/desktop/node_modules/"

# 2) 在该 profile 的 cordis.patch.yml 末尾追加两行：
#    - insert:
#        - id: mcd-float
#          name: 'dsh-mcd-float'

# 3) 重启桌面 App
```

Web 形态（`dsh web`）无需额外配置，插件会通过 `tapIndex` 自动注入。

## 结构

```
dsh-mcd-float/
├── lib/index.js          服务端：注册 /dsh-mcd-float/float.js 与 /dsh-mcd-float/boot，
│                                 并往页面注入客户端脚本
├── assets/float.js       浏览器端：悬浮球本体
├── cordis.patch.yml      bundle patch
└── package.json
```

## 三个值得说的实现细节

1. **注入用内联 script 行，不用 `script-src`**
   桌面端（Electron）只认 `webserver/index-inject` 推的结构化行；若用 `kind:'script-src'`，
   加载失败会 reject 掉 `__DSH_BOOT_READY__`，**直接让整个应用起不来**。
   这里用 `kind:'script'` 内联行，由注入代码自己建 `<script src>` 并吞掉 `onerror`——
   路由在就正常加载，路由不在就静默失败，永远不会拖垮宿主。

2. **先开窗口，后导航**
   `window.open` 必须在用户手势的同步调用栈里。若先 `await` 探测或启动服务再开窗，
   手势上下文丢失，弹窗必被浏览器拦截。所以先开空白窗口，就绪后再 `win.location.href = url`。

3. **样式全走 CSSOM**
   不用 `<style>` 标签（会被宿主 CSP 的 `style-src` 拦），一律 `el.style.xxx = ...`。

## 已知限制

- 宿主 CSP 为 `default-src 'self'`，**跨端口的 iframe 可能被 `frame-src` 拦**，
  所以「应用内打开」不保证可用；「复制地址」永远可用
- 服务端口需在 `8808~8812`；改 `assets/float.js` 的 `PORTS` 可扩展
- 只实现了 Windows 的启动路径（`python` / `py` 双候选）；macOS/Linux 用 `python3`

## 许可

MIT
