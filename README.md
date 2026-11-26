# MoveCar Lite

轻量级多用户挪车通知系统。功能对齐 `nbbk/MoveCar-Docker-NB`，但**不用 Node.js、不用 Redis**。

- 运行时：Python 3.13 标准库（`http.server`）+ SQLite
- 第三方依赖：**零**。唯一外部库 `segno`（纯 Python 二维码）已 vendor 在 `vendor/`
- 容器：**1 个**（原版 2 个），无 Redis、无 pub/sub，实时同步用 SSE
- 前端：4 个单文件 HTML + 原生 JS（无构建、无 CDN）
- 数据：SQLite 单文件 `data/movecar.db`，备份 = 拷一个文件

## 功能

| 模块 | 说明 |
|---|---|
| 扫码呼叫页 `/?u=标识` | 选原因 → 可选留言 → 通知车主；未授权定位进 30 秒罚站（服务端强制）；成功后展示「紧急电话联系车主」按钮（`tel:` 直拨） |
| 车主端 `/owner-confirm?u=标识` | 待办列表 + 高德/苹果导航 + 标记已处理；SSE 实时到达并响铃震动 |
| 海报页 `/qr?u=标识` | 3 款海报（暗夜金 / 薄荷绿 / 警示黑黄），canvas 1080×1440 本地渲染，一键保存；支持 `&t=0|1|2` 直达样式 |
| 管理后台 `/admin` | 密码登录；概览 / 车主与通道 / 呼叫记录 / 设置 |
| 推送通道 | WxPusher（uid 或 topicId，HTML 富文本，处理入口是可点击的「点击处理挪车请求」）、SMTP 邮件（同样是超链接）、企业微信 / 钉钉群机器人（Markdown，链接可点）、通用 Webhook（自定义 JSON） |
| 防骚扰 | 服务端签发呼叫凭证 + 未定位罚站 N 秒 + 同 IP 同车主间隔限制 |

## 部署

### 方式一：从开发机一键推

```bash
export MOVECAR_HOST=192.168.1.10      # 目标机地址
export MOVECAR_USER=admin             # SSH 账号
export MOVECAR_PASS='SSH密码'          # 仅用于 sudo
python deploy/deploy.py --password '你的后台密码'     # 首次
python deploy/deploy.py                              # 后续（沿用 .env 里的密码）
python deploy/deploy.py --selftest                   # 只做远程自检
```

目标机目录 `$MOVECAR_HOST:/vol1/1000/docker/movecar_lite`，端口 `13100`。

### 方式二：目标机上手动

```bash
cd /vol1/1000/docker/movecar_lite
echo 'ADMIN_PASSWORD=你的密码' > .env && chmod 600 .env
sudo docker compose up -d --build
```

### 方式三：装成飞牛 fnOS 应用（.fpk，推荐给「别人的 NAS」）

`fnos/` 目录就是这个应用的 fnOS 打包工程，产物在 `outputs/movecarlite_<版本>_<架构>.fpk`。

**打包**（改完 `app.py` / `static/` 后重新生成）：

```bash
cd fnos
export MOVECAR_HOST=192.168.1.10 MOVECAR_USER=admin MOVECAR_PASS='SSH密码'
python build.py --remote            # 上传到 NAS 用官方 fnpack 打包（cmd 脚本执行位才对）
python build.py                     # 本机打包（Windows 上 cmd 权限位可能丢，仅供快速验证）
# 需要改图标时跑：pwsh -File make_icons.ps1
```

**在任意飞牛 NAS 安装**：应用中心 → 右上角「手动安装」→ 选 `.fpk` → 安装向导里填**访问端口**和**管理密码**。
装完桌面出现「挪车助手」图标，点开即管理后台。

**桌面入口走「统一网关」**：入口是 `/app/movecarlite/...`（和飞牛自带应用 Moo / 终端 / Hermes 一样），由 fnOS 自己的 web 服务经 `@appcenter/movecarlite/app.sock` 转发，**不依赖端口**。所以内网直连、飞牛中继、自己的反代域名，点图标都不会白屏。端口 `18100` 仍然照常监听，可直接访问 `http://NAS:18100/admin`，也能单独给它做反代。

> 实现要点：页面里的资源与接口全是**相对地址**，服务端按请求来源注入 `<base href="/">` 或 `<base href="/app/movecarlite/">`；同时后端会剥掉 `/app/movecarlite` 前缀再路由，所以两种访问方式共用一套代码。

| 项 | 说明 |
|---|---|
| 运行时 | 直接用系统自带 `/usr/bin/python3`（fnOS = Debian 12，Python 3.11），不下载任何运行时 |
| 入口 | 网关 `/app/movecarlite`（要 fnOS ≥ 1.1.3100，manifest 已声明 `os_min_version`）；端口直连保留 |
| 端口 | 向导里填，默认 `18100`；改端口在「应用设置 → 修改配置」里，服务自动重启 |
| 密码 | 向导里填；后台「设置 → 改密码」也会写回 `@appdata/movecarlite/admin.password`，重启不丢 |
| 数据 | `/vol{n}/@appdata/movecarlite/data/movecar.db`（SQLite 单文件） |
| 升级 | 在应用中心重新上传更高版本号的 fpk，`@appdata` 里的数据不动。⚠️ `appcenter-cli install-fpk` 对已安装的应用只回「is installed」不升级，必须走 Web UI；实在不行就卸载（向导选「保留数据」）再装 |
| 卸载 | 卸载向导可选「保留数据 / 删除数据」；带向导的应用**只能在应用中心图形界面卸载**，CLI 会报 requires custom wizard parameters |
| 架构 | `platform = all`，x86 与 arm 通用（纯 Python，无二进制） |

迁移已有数据：把老部署里的 `data/movecar.db` 拷到 `/vol{n}/@appdata/movecarlite/data/` 后重启应用即可。

## 推送通道怎么填

后台「车主 → 编辑 → 推送通道 → 添加通道」，类型下拉里有五种：

| 类型 | 需要填 | 说明 |
|---|---|---|
| `wxpusher` | appToken + `UID_xxx` 或 topicId | HTML 富文本，正文里的「点击处理挪车请求」可点 |
| `email` | 收件邮箱（留空用车主邮箱） | 走「设置」里的 SMTP |
| `wecom` | 企业微信群机器人地址 + 可选格式 | `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…`，默认发 **news 图文卡片**（微信插件里能显示、点卡片直达处理页） |
| `dingtalk` | 钉钉群机器人地址 | `https://oapi.dingtalk.com/robot/send?access_token=…`，发 Markdown |
| `webhook` | 任意地址 | POST 我们自己定义的 JSON；**如果地址是企业微信/钉钉的机器人，会自动按机器人格式发送** |

### 企业微信要特别注意「消息类型」

- **`markdown` 消息在微信插件里不显示**（在微信中收企业微信消息时提示「暂不支持消息类型」），所以默认不用它。
- 默认发 `news` 图文卡片：标题 + 车辆/时间/位置/留言 + `url`，点击卡片直接进处理页。
- 如果卡片也显示不了，后台该通道的「消息格式」填 `text` 即可（纯文本，链接会附在末尾，长按可复制）。
- 图文失败时程序会自动退回纯文本再试一次，不会静默丢消息。

⚠️ 企业微信和钉钉的机器人**失败时也返回 HTTP 200**，必须看返回体里的 `errcode`。程序已经这样判定，所以后台「发测试通知」显示的成败是真实的，不会出现「提示成功但群里没消息」。

## ⚠️ 上线必读

1. **GPS 必须要 HTTPS**。浏览器只在安全上下文允许 `geolocation`；用 `http://IP:13100` 打开时定位一定失败，扫码页会走 30 秒罚站路径（功能仍可用，但没有位置和导航）。正式使用请在 Lucky / nginx 上把域名反代到 `http://你的NAS地址:13100` 并配好证书，然后在后台「设置 → 外网主域名」填 `https://你的域名`——二维码和推送里的链接都取这个值。
2. **后台密码**在 `docker-compose.yml` 同目录的 `.env` 里（权限 600），不在仓库内。改密码：改 `.env` → `sudo docker compose up -d --force-recreate`。不设 `ADMIN_PASSWORD` 时程序会随机生成一个并打印在容器日志里（重启会变），所以务必显式设置。后台里的「修改密码」只有在设置了密码文件（`PASSWORD_FILE`，fpk 安装包就是这样）时才会持久化。
3. **备份**：把 `data/` 整个拷走即可（含 `movecar.db`，WAL 模式下还有 `-wal`/`-shm`，最好停容器再拷，或 `sqlite3 data/movecar.db ".backup ..."`）。
4. 后台不能长时间挂着默认密码——它和 9 个推送通道一样是公网入口。

## 目录

```
app.py                 单文件服务（路由/DB/推送/SSE/二维码）
static/app.css         共用样式
static/call.html       扫码呼叫页
static/owner.html      车主接单页
static/poster.html     海报页
static/admin.html      管理后台
vendor/segno/          二维码（纯 Python，已内置）
deploy/deploy.py       推送部署脚本
Dockerfile             python:3.13-slim，无 pip install
docker-compose.yml     单服务 + ./data 挂载
fnos/                  fnOS 应用包工程（manifest / cmd / wizard / build.py → .fpk）
outputs/*.fpk          打好的安装包
```

## 与原版的差异

保留：多车主、推送通道、罚站机制、海报、导航、状态同步、管理后台。

新增（原版没有）：**车主手机号**。`/api/pub` 不返回它，只有在「已发起呼叫」的 `/api/call` 响应里才下发，扫码页据此显示「紧急电话联系车主」直拨按钮——避免贴个二维码就把号码公开给所有路人。

改变：
- 存储从 Redis 换成 SQLite（Redis 那套本质是键值 + pub/sub，这里用表 + 进程内事件）
- 服务必须单进程（SSE 总线在内存），`--workers 1` 同类约束，两人同时扫也不会有问题
- 海报从 15 款降到 3 款，模板在 `poster.html` 里是纯 canvas 绘制函数，加款只需加一个 `tplXxx(ctx)` 函数并注册进 `TPLS`
- 未做「JSON 模板热更新」——那是给非程序员改文案用的，代码里改更快

## API 速查

```
GET  /api/pub/{code}           车主公开信息 + 罚站秒数 + 原因列表
POST /api/challenge            {code, loc_ok} → {challenge, cooldown}
POST /api/call                 {code, challenge, reason, note, loc:{lat,lng,accuracy}}
                               → {ok, id, pushed, channels, message, phone}
GET  /api/calls/{code}         呼叫列表（近 30 条）
POST /api/ack                  {code, id}
GET  /api/stream/{code}        SSE：call / ack
GET  /api/qr/{code}.png|.json  二维码图片 / 模块矩阵
POST /api/admin/login          {password} → token（同时写 Cookie）
GET  /api/admin/state          设置 + 车主 + 记录 + 统计
POST /api/admin/settings       更新设置
POST /api/admin/owner          新建/更新车主（带 id 为更新）
POST /api/admin/owner/delete   {id}
POST /api/admin/owner/test     {id} 发测试通知
```
