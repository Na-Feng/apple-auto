# iCloud 隐私邮箱（Hide My Email）创建实现文档

> 对象仓库：
> - `iCloud-Privacy-Mail`（Go 全协议后端，`q1953258942/iCloud-Privacy-Mail`，`AppVersion 2026.07.02.2`）
> - `hme-manager`（Python 会话导入式，`Wei-l-w/hme-manager`）
>
> 本文只描述公开源码里的实现方式，不含真实账号、Cookie、`scnt`、API Key、验证码或运行数据。

---

## 1. 先说结论：隐私邮箱到底怎么"创建"

"隐私邮箱"（Hide My Email，下称 HME）是 iCloud+ 功能，本质是 Apple 账号下的一个转发别名 `xxxxx@icloud.com`。它**没有独立密码、不能登录 IMAP**，邮件只会转发到你绑定的真实收件箱——这一点直接决定了两个项目都必须额外接一个真实收件箱才能收验证码。

自动化创建 HME 只有两条通道，两个项目都围绕它们做工程化：

| 通道 | 代号 | 端点族 | 需要的凭证 | 面板口径限额 |
| --- | --- | --- | --- | --- |
| iCloud Web 旧接口 | `icloud_web` | `{premiumMailBaseURL}/v1/hme/*`、`/v2/hme/*` | iCloud WebServices Cookie + `dsid` + `clientId` + build 号 | 约 5 个/小时 |
| Apple Account 新接口 | `apple_account` | `appleid.apple.com/account/manage/email/private/*` | 管理态 Cookie + `scnt` + 动态 `X-Apple-Api-Key` | 约 20 个/小时 |

两条通道的差别不只是端点，而是**凭证寿命模型**不同：

- 旧接口：完成 `2sv/trust` + `accountLogin(extended_login=true)` 后拿到 iCloud WebServices 长效 Cookie，能长期不重新登录。
- 新接口：拿到的是**短期活动窗口**。Apple 通过 `gs/ws/token` 下发 `timeOutInterval`，到期必须刷新 `scnt`/Cookie/`apiKey`，否则创建会 401/419。

`iCloud-Privacy-Mail` 两条都自己实现了（默认优先新接口、失败回落旧接口）；`hme-manager` **只实现旧接口**，把"登录"整体外包给人工（浏览器复制 cURL）。

### 1.1 两个项目一句话定位

| | iCloud-Privacy-Mail | hme-manager |
| --- | --- | --- |
| 定位 | 什么都自己干的重实现 | 只干创建+收信、登录交给人的轻实现 |
| 登录 | 自己实现 Apple SRP 协议登录 | 零登录，导入浏览器会话 |
| 语言/依赖 | Go 标准库，前端 embed 进二进制 | Python 3.10+，**零第三方依赖** |
| 通道 | 新接口 + 旧接口 | 仅旧接口 |
| 批量 | 多账号并发 + 定时调度器 | 单账号持久化 SQLite 队列 |
| 收码 | iCloud IMAP（自建 DNS/TLS 拨号） | 转发收件箱 IMAP（默认 Gmail）+ 本地缓存 |
| 多用户 | 有（管理员/普通用户，按 owner_id 隔离） | 无（单租户，`HME_API_KEY` 保护） |

两者对 **HME 创建协议本身**的理解是一致的：`generate` → `reserve`（旧接口）。

---

## 2. 项目 A：iCloud-Privacy-Mail（Go 全协议后端）

### 2.1 架构与文件职责

```
cmd/panel/main.go           进程入口：加载配置 → 打开 FileStore → 启后台 worker → ListenAndServe
internal/app/
  config.go                 配置加载（文件 + 环境变量）与默认值
  models.go                 State / User / Account / Mailbox / Message / ICloudSession / LoginState
  store.go                  单文件 JSON 存储，全局互斥锁，写操作整体落盘
  auth.go                   平台自身账号体系（PBKDF2-SHA256 密码、会话令牌哈希）
  server.go                 路由、HTTP 处理、创建编排、取码 API、后台同步器（约 4700 行）
  scheduler.go              定时批量创建任务
  apple_srp.go              SRP-6a 客户端（Apple 变体：s2k / s2k_fo）
  apple_auth_client.go      Apple ID 协议登录（旧接口 + 新接口两条链路）
  icloud_client.go          iCloud Web 接口 + Apple Account 管理接口 + 浏览器指纹伪造
  icloud_validate.go        setup/ws/1/validate 登录态校验
  imap_client.go            iCloud IMAP：自建 DNS/TLS 拨号、IDLE、UID 增量抓取
  updater.go                在线更新（GitHub Release / manifest）
internal/app/templates/      三个前端页面（index / login / manage）
```

数据流：**面板或 API → Server → ICloudClient / AppleAuthClient → Apple → FileStore 落盘**。

### 2.2 旧接口（icloud_web）登录链路

入口 `POST /api/icloud/protocol-login/start` → `AppleAuthClient.StartLogin` → `startLoginOnHost`。

**第 0 步：选端点集。** 按 `icloud_default_host` 决定国内还是国际：

| | 国内 | 国际 |
| --- | --- | --- |
| Home | `https://www.icloud.com.cn` | `https://www.icloud.com` |
| Setup | `https://setup.icloud.com.cn/setup/ws/1` | `https://setup.icloud.com/setup/ws/1` |
| Auth | `https://idmsa.apple.com.cn/appleauth/auth` | `https://idmsa.apple.com/appleauth/auth` |

同时生成随机 `frame_id`（UUID 小写），客户端 ID 取配置 `icloud_client_id`，默认 `d39ba9916b7251055b22c7f910e2ea796ee65e98b2ddecea8f5dde8d9d1a815d`。

**第 1 步：authStart** —— 拿 Apple 下发的 Cookie 与挑战：

```http
GET {Auth}/authorize/signin
    ?frame_id=auth-<uuid>&skVersion=7&iframeId=auth-<uuid>
    &client_id=<clientId>&redirect_uri=<Home>
    &response_type=code&response_mode=web_message&state=auth-<uuid>
    &language=zh_CN&authVersion=latest
```

响应头里的 `scnt`、`session` 等由 `session.extract(resp)` 记录进内存会话。

**第 2 步：authFederate**

```http
POST {Auth}/federate?isRememberMeEnabled=true
{"accountName":"<appleId>","rememberMe":true}
```

**第 3 步：authSRP —— 密码验证的核心**

```http
POST {Auth}/signin/init
{"a":"<base64(A)>","accountName":"<appleId>","protocols":["s2k","s2k_fo"]}
→ {"iteration":N,"salt":"...","protocol":"s2k_fo","b":"...","c":"..."}
```

本地派生与计算（`apple_srp.go`）：

```
derived = PBKDF2-HMAC-SHA256(password, salt, iteration)   // 按 protocol 选 s2k / s2k_fo
x  = H( salt | H( user | ":" | derived ) )
k  = H( N | PAD(g) )
u  = H( PAD(A) | PAD(B) )
S  = (B - k * g^x) ^ (a + u*x)  mod N
K  = H(S)
M1 = H( H(N) ^ H(g) | H(user) | salt | A | B | K )
M2 = H( A | M1 | K )
```

```http
POST {Auth}/signin/complete?isRememberMeEnabled=true
{"accountName":"<appleId>","m1":"...","m2":"...","c":"...",
 "rememberMe":true,"trustTokens":["<trustToken 若有>"]}
```

状态码语义：
- `409 Conflict` → **需要 2FA**，`needs2FA = true`
- `401 Unauthorized` → 映射为 `apple_credentials_invalid`（账号或密码错误）
- `403 Forbidden` → 映射为 `apple_login_forbidden`

**第 4 步：2FA 分支**

| 方式 | 触发请求 |
| --- | --- |
| `trusted_device`（默认） | `PUT {Auth}/verify/trusteddevice/securitycode`（无 body） |
| `phone` / `sms` | `PUT {Auth}/verify/phone` `{"phoneNumber":{...},"mode":"sms"}` |

两种方式都把整个 `*appleAuthSession` 放进 `appleAuthPendingStore` 并返回 `pending_id`。**该 pending 只存在进程内存，TTL 10 分钟**，服务重启即失效。

**第 5 步：提交验证码** `POST /api/icloud/protocol-login/2fa` → `Submit2FA`

```http
POST {Auth}/verify/trusteddevice/securitycode
{"securityCode":{"code":"123456"}}
```

然后三个关键动作：

```http
GET  {Auth}/2sv/trust                 ← 长效 Cookie 的关键，带瞬时错误重试
POST {Setup}/accountLogin
{"accountCountryCode":"<country>","dsWebAuthToken":"<sessionToken>",
 "extended_login":true,"trustToken":"<trustToken>"}
POST https://setup.icloud.com[.cn]/setup/ws/1/validate
     ?clientBuildNumber=2622Build20&clientMasteringNumber=2622Build20
     &clientId=<uuid>&requestId=<uuid>
```

`validate` 的响应决定后续一切，落库字段：

| 响应字段 | 落库字段 | 含义 |
| --- | --- | --- |
| `dsInfo.dsid` | `DSID` | 账号数字 ID，所有旧接口都要带 |
| `dsInfo.appleId` | `AppleID` | 展示用 |
| `dsInfo.isHideMyEmailSubscriptionActive` | `IsICloudPlus` | 是否 iCloud+ |
| `dsInfo.isHideMyEmailFeatureAvailable` | `CanCreateHME` | 是否有创建权限 |
| `webservices.premiummailsettings.url` | `PremiumMailBaseURL` | **旧接口 base URL** |
| `webservices.mccgateway.url` | `MailGatewayBaseURL` | 邮件网关 |
| `webservices.mail.url` | `MailBaseURL` | 邮件服务 |

**区域跳转**：Apple 可能返回 3xx + `{"domainToUse":"iCloud.com.cn"}`。`parseAppleDomainRedirect` 解析后 `switchHost` 切端点并重试一次（`Submit2FA` 最多两次循环）；`accountLogin` 返回的 `accountCountryCode` 也会触发同样的跳转。

### 2.3 新接口（apple_account）登录链路

入口 `POST /api/apple-account/login/start` → `StartAppleAccountManageLogin`。与旧接口的差异：

| 步骤 | 旧接口 | 新接口 |
| --- | --- | --- |
| Home | `www.icloud.com[.cn]` | `https://account.apple.com` |
| OAuth client_id | `d39ba991...`（iCloud Web） | `af1139274f266b22b68c2a3e7ad932cb3c0bbe854e13a79af78dcc73136882c3` |
| UserAgent | Safari 18.3.1 | Chrome 149 / macOS |
| 预热 | 无 | 先开 `/account/manage/section/privacy` + `/bootstrap/portal`，再取 `scnt` |
| authorize 参数 | `authVersion=latest` | `authVersion=8.0.2` + `Sec-Fetch-Dest: iframe` |
| 额外前置 | 无 | `POST {Auth}/verify/device/key/challenge` `{"passkeyAutofill":false}` |
| signin/complete | 直接提交 | 必须先算 **hashcash**，加 `X-Apple-HC` 头 |
| 登录后 | `accountLogin` + `validate` | 刷新管理态拿 `apiKey` |

**hashcash 细节**（`generateAppleHashcash`）：从 `authStart` 响应头读 `X-Apple-HC-Bits` / `X-Apple-HC-Challenge`，构造

```
1:{bits}:{yyyyMMddHHmmss UTC}:{challenge}::{counter(base36)}
```

对整串做 SHA-1，要求前导零比特数不小于 `bits`。硬上限 `appleHashcashMaxBits = 24`、`appleHashcashMaxAttempts = 2^24`，超限返回 `apple_hc_too_hard`。

**登录成功后的状态构造**（`authWithAppleAccountManage`）：

```go
loginState := LoginState{
    Kind: LoginStateAppleAccount, Host: "appleid.apple.com",
    Origin: "https://account.apple.com",
    Cookies: session.cloneCookies(),
    Scnt: firstNonEmpty(session.Scnt, session.ManageScnt),
    SessionID: session.SessionID, UserAgent: appleAccountManageUserAgent,
}
refreshed, err := client.RefreshAppleAccountManageState(ctx, loginState)
```

即**新接口登录态必须经过一次刷新拿到 `apiKey` 才可用**。

### 2.4 登录态数据结构与持久化

`ICloudSession` 下挂 `LoginStates []LoginState`，按 `Kind` 分三类：

| Kind | 用途 | 关键字段 |
| --- | --- | --- |
| `icloud_web` | 旧接口创建 + 同步已有邮箱列表 | `Cookies`、`Host`、`Origin` |
| `apple_account` | 新接口创建 | `Scnt`、`SessionID`、`APIKey`、`ManageExpiresAt`、`Cookies`、`UserAgent` |
| `icloud_imap` | 收验证码 | `IMAPEmail`、`IMAPAppPassword`、`IMAPHost/Port`、`IMAPLastSyncUID` |

三个 Kind **互不覆盖**：`mergeLoginStates` 按 `Kind` 逐个替换或追加。同一 Apple 账号可同时持有三份状态，所以"新接口 + 旧接口都保存后可以一起创建，合计约 25 个/小时"。

持久化：`FileStore` 一把 `sync.Mutex`，每次写操作 `saveLocked()` 全量 JSON 落盘到 `data_path`。合并已有会话时 `mergeICloudSession` 做逐字段 `firstNonEmpty` 兜底，避免一次刷新失败把已保存的 Cookie/`scnt` 清空。

> 安全提示：`LoginState.IMAPAppPassword` 是明文存在 `state.json` 里的，只靠文件权限保护（README 建议 `chmod 600`）。

### 2.5 创建隐私邮箱（核心）

#### 2.5.1 调用链

```
POST /api/icloud/mailboxes/create           handleCreateICloudMailbox
  └─ createMailboxesForOwnerWithChannels    多账号并发，每账号一个 goroutine
       └─ createMailboxForOwner
            └─ createICloudMailboxRemoteWithChannel
                 ├─ 账号级闸门 acquireMailboxCreateGate + waitMailboxCreateInterval（最小 3 秒）
                 ├─ channel=apple_account → createICloudMailboxRemoteAppleAccount
                 ├─ channel=icloud_web    → createICloudMailboxRemoteICloudWeb
                 └─ channel=auto（默认）  → 先新接口，失败回落旧接口
```

`auto` 的回落判定：只要会话里存在 `apple_account` 登录态就先试新接口；新接口报错且**不是** `context.Canceled/DeadlineExceeded` 时记下错误并 warn，接着走旧接口；两边都失败返回组合错误码 `mailbox_create_all_channels_failed`，消息里同时带两条通道的原始错误。

#### 2.5.2 新接口：两段式 POST/PUT + 一次 GET 确认

```http
POST /account/manage/email/private/add
X-Apple-Api-Key: <apiKey>
scnt: <scnt>
Cookie: <管理态 Cookie>
Origin: https://account.apple.com
Referer: https://account.apple.com/
X-Apple-I-FD-Client-Info: {"U":"<UA>","L":"zh","Z":"GMT+08:00","V":"1.1","F":"<压缩指纹>"}
X-Apple-I-Request-Context: ca
X-Apple-I-TimeZone: Asia/Shanghai
{}
→ 200 {"emailAddress":"xxxxx@icloud.com"}
```

```http
PUT /account/manage/email/private/add/complete
{"emailAddress":"xxxxx@icloud.com","label":"<标签>","note":"<备注>"}
→ 200 {"emailAddress","label","note","id","active"}
```

```http
GET /account/manage/email/private/{id}.em        ← 可选确认，失败不影响创建结果
→ {"emailAddress","label","note","id","forwardToEmail","active"}
```

要点：
- 没有 `generate`/`reserve` 两步：`add` 是"生成候选"，`add/complete` 才是"占用"。候选若不 complete，不会真正创建。
- `label` 为空时自动生成 `UPI-MMDD-HHMMSS`。
- 返回的 `id` 存为 `ICloudRemoteMailbox.AnonymousID`，`Origin` 标记 `APPLE_ACCOUNT`。
- 成功后 `markAppleAccountManageOK` 刷新 `LastCheckedAt`，更新后的 `LoginState` 回写并 `SaveICloudSessionForOwner` 落盘。

**失败后的一次自动重试**（`CreatePrivacyMailboxWithAppleAccount`）：
1. 若 `ManageExpiresAt` 已过或 `apiKey` 为空，创建前先刷新一次。
2. 若创建返回 `apple_account_auth_failed` 且**之前没刷新过**，再刷新一次并重试创建。

这条规则让"登录态刚过期"不会直接失败，又不会无限重试。

#### 2.5.3 旧接口：generate + reserve

前置校验：`PremiumMailBaseURL`、`DSID`、`Cookies` 三者非空，且 `IsICloudPlus && CanCreateHME` 为真；否则直接返回 `icloud_session_missing` / `icloud_hme_unavailable`。

```http
POST {PremiumMailBaseURL}/v1/hme/generate
     ?clientBuildNumber=2622Build20&clientMasteringNumber=2622Build20
     &clientId=<clientId>&dsid=<dsid>
Content-Type: text/plain;charset=UTF-8
{"langCode":"zh-cn"}
→ {"success":true,"result":{"hme":"xxxxx@icloud.com"}}
```

```http
POST {PremiumMailBaseURL}/v1/hme/reserve?<同样的查询参数>
{"hme":"xxxxx@icloud.com","label":"<标签>","note":"<备注>"}
→ {"success":true,"result":{"hme":{"anonymousId","hme","label","note",
                                   "forwardToEmail","isActive"}}}
```

响应统一是 `{"success":bool,"result":{...},"error":{...}}` 信封，由 `callEnvelope` 解析。`success=false` 时经 `iCloudAPIError` 归类：消息含 `reached the limit of addresses`、或 `limit` + `try again later`、或 `创建上限` → `icloud_hme_limit`。

请求头由 `setICloudFetchHeaders` 统一设置：Chrome 143 / Windows UA、按 host 自动选 `.cn` 或国际站的 `Origin`/`Referer`、`Sec-Fetch-*`、`Sec-CH-UA-*`。

#### 2.5.4 限流、冷却与并发控制

三层保护：

| 机制 | 函数 | 行为 |
| --- | --- | --- |
| 账号级串行闸门 | `acquireMailboxCreateGate` | 每个 `ownerID:accountID` 一个容量 1 的 channel，同账号同时只发一个创建请求 |
| 最小间隔 | `waitMailboxCreateInterval` | `mailboxCreateMinInterval = 3s`，距上次成功不足则等待 |
| 限流冷却 | `markMailboxCreateCooldown` | 命中 `apple_account_hme_limit` 或 `icloud_hme_limit` 时，按**通道分别**冷却 2 分钟 |

冷却 key 形如 `{ownerID}:{accountID}:cooldown:{channel}`，所以新接口被限流不会连带封掉旧接口。冷却期内旧接口直接前置返回 `icloud_hme_limit`（带剩余秒数），新接口不做前置拦截而是照发、失败后再冷却。

#### 2.5.5 定时调度器

`runMailboxScheduler` 是无限循环：每轮 → `runMailboxSchedulerBatch` → 等 `Interval`（默认 60 分钟）→ 下一轮。

单轮逻辑实现了"失败账号只临时跳过"：

- 维护四张表：`disabledChannels[accountID][channel]`（本批次已废通道）、`skippedThisBatch[accountID]`、`oneShotNextChannels[accountID]`（下轮只试这个通道）、`transientRetriedChannels`（瞬时错误只重试一次）。
- 循环 `index = 1,2,3...`，算出活跃请求；为空就记录"剩余账号都已临时跳过"并结束本轮。
- 失败分类：
  - **瞬时错误**：第一次 → 记 `channel_failed` + 下轮单通道重试；第二次 → 废该通道换下一个；没得换就跳过该账号。
  - **非瞬时错误** → 直接废通道换下一个；都没了就跳过该账号。
  - **成功** → 清除该账号的 `oneShotNextChannels` / `transientRetriedChannels`。
- 只要还有活跃请求，就等 `RoundInterval`（默认 5 秒）继续下一轮。

调度状态与事件日志（最多 200 条）只在进程内存，前端每 3 秒轮询 `/api/icloud/scheduler/status`。

### 2.6 新接口保活：能不能长期用的关键

`refreshAppleAccountManageStateUnlocked` 的完整顺序：

```
1. GET {appleid}/account/manage/gs/ws/token
     → 响应体 timeOutInterval（分钟）→ ManageExpiresAt = now + timeOutInterval
     → 若 timeOutInterval <= 0：先 warmAppleAccountPortal，再"去掉 scnt 重试"，仍不行带 scnt 再试
2. GET {appleid}/account/manage        → 读动态 apiKey
     → 成功即 markAppleAccountManageOK 并返回
3. 第 2 步失败时：
   a. warmAppleAccountPortal：GET /account/manage/section/privacy + GET /bootstrap/portal
   b. 去掉 scnt 重试 gs/ws/token；失败则恢复原 scnt 再试
   c. 再读 /account/manage 拿 apiKey；仍失败 → 返回错误
```

`keepAliveAppleAccountManageStateUnlocked` 在此之上再加两步"证明管理态真能用"：

```http
GET  /account/manage/forwardemail      ← 真实管理接口，401 即说明登录态已死
POST /v2/jslogs  [{"eventId":...,"eventType":"custom","componentName":"performance",...}]
                                       ← 仅补充浏览器会话头，失败可忽略
```

后台 worker 默认每 240 秒（`APPLE_ACCOUNT_KEEP_ALIVE_MS`）扫一轮，扫描周期 `min(base/4, 30s)`，对到点账号错峰执行。**已检测异常的登录态会被跳过**，避免反复无效保活。

> 变更日志记录了一个重要结论：`/v2/jslogs` 持续返回新会话标识**并不能**证明管理接口可用；真实入口 `/account/manage/email/private` 和 `/account/manage/forwardemail` 仍可能在页面开着时返回 401。所以保活必须以真实管理接口为准。

### 2.7 收验证码：iCloud IMAP

创建用的登录态与收码用的登录态是**分开的**。收码走 `icloud_imap`：

- 保存入口 `POST /api/icloud/imap-login/save`，先 `CheckICloudIMAPLogin` 实测一次 `LOGIN` + `SELECT INBOX` 才落库。
- 主机固定 `imap.mail.me.com:993`，凭据是 iCloud 邮箱账号 + **App 专用密码**。
- 拨号器 `dialICloudIMAPTLS` 做了三级兜底，专门解决国内服务器 DNS 53 端口超时：
  1. 硬编码 IPv4 列表（7 个地址）直连；
  2. 公共 DNS over **TCP**（`1.1.1.1:53`、`8.8.8.8:53`、`127.0.0.53:53`）解析后直连；
  3. 正常域名拨号。
  直连时仍用 `imap.mail.me.com` 作为 TLS SNI。
- 增量抓取 `SyncICloudIMAPMessagesWithCursor`：
  - 搜索语句优先级：账号级 `IMAPLastSyncUID` → `UID SEARCH UID {uid+1}:*`；否则取各邮箱 `LastSyncUID` 最小值 → `UID SEARCH UID {n+1}:*`；都没有才回退 `UID SEARCH SINCE {date}`。
  - `UID FETCH <set> (UID BODY.PEEK[]<0.200000>)`，按 20 个一批；`BODY.PEEK` 不会把邮件标记为已读。
  - 本地匹配：先 `looksLikeVerificationText` 过滤，再用 `matchingMailboxIDs` 在**全部邮件头**里做大小写无关包含匹配（`imapHeaderText` 把所有 header 拼成一个大字符串，因此 `Delivered-To` / `X-Original-To` 这类转发头也能命中）。
- 实时监听：`WatchICloudIMAPExists` 常驻 `IDLE`，收到 `EXISTS` 就回调触发一次增量同步。启动时先 `LatestICloudIMAPUID` 读 `SELECT` 响应里的 `UIDNEXT` 减 1 作为起跑线，**不预抓历史邮件**。
- 后台另有 3 秒一轮的兜底轮询，以及"最近被取码 API 访问过的邮箱排到本轮最前并立即唤醒"的加速机制。

### 2.8 对外取码 / 取号 API

| 端点 | 鉴权 | 说明 |
| --- | --- | --- |
| `GET /api/v1/mailboxes/{email}/code` | `?key=<mailbox_key>` | 单邮箱取码 |
| `GET /api/mailboxes/{id}/code` | `?key=<mailbox_key>` | 同上，按内部 ID |
| `POST /api/v1/mailboxes/claim` | `Authorization: Bearer <全局 api_key>` | 自动取号，领走即置 `used` |
| `POST /api/v1/mailboxes/lookup` | 同上 | 批量查 1–500 个邮箱的 API 信息 |
| `GET /api/v1/health` | 同上 | 健康检查 |

取码逻辑顺序：

1. 鉴权 → `APIActive` / `Status != disabled` / `ICloudActive` 三重校验。
2. `markMailWatcherActive` 把这个邮箱插队到下一轮同步最前。
3. `after` 参数经 `mailboxCodeAfter` 夹到 `now - 5min`（`mailboxCodeFreshWindow`）。
4. `cache=1` 只读本地缓存直接返回。
5. 本地命中就直接返回；`skipMessageID` 是上次已发放的邮件 ID，**同一封邮件默认不会重复发放**。
6. 未命中 → `waitMailboxCode` 触发快速补抓，默认最多等 600ms（`public_fast_sync_wait_ms`），带 `wait_ms` 最多 30 秒。
7. 同步失败时：`allow_stale=1` 才回退本地旧码；否则返回 `mail_sync_failed` 502，**拒绝把旧码当新码发出去**。

验证码提取 `extractOTP` 只有两条正则：

```go
contextOTPRegex = `(?i)(?:openai|chatgpt|otp|code|verification|验证码|验证|代码)[^\d]{0,80}(\d{6})`
plainOTPRegex   = `\b(\d{6})\b`
validOTP: len == 6 && code != "000000"
```

每个邮箱的 `api_token` 是 `randomToken(24)`，存在 `mailboxes[].api_token`，重启与升级都不会变。

### 2.9 安全与脱敏

- 密码只参与当次登录请求，**不落盘、不回前端、不写日志**。
- 调试开关 `IPM_DEBUG_APPLE_ACCOUNT=1` / `IPM_DEBUG_APPLE_AUTH=1` 打开后，日志只输出 method/path、状态码、Cookie 长度、`scnt` 的 SHA-1 指纹（前 4 字节 + 长度）、`Set-Cookie` 数量、脱敏后的 body（`apiKey`/`token`/`session`/`password`/`code` 等 key 一律替换）。
- 前端只回 `APITokenMask`（保留末 6 位），完整 token 只在导出的 API 列表里。
- 全局 `api_key` 只接受请求头，不接受 URL 查询参数。

### 2.10 关键常量速查

| 常量 | 值 | 位置 |
| --- | --- | --- |
| `mailboxCreateMinInterval` | 3s | server.go |
| `mailboxCreateLimitCooldown` | 2min | server.go |
| `mailboxCodeFreshWindow` | 5min | server.go |
| `mailboxCodeFastWait` / `MaxClientWait` | 600ms / 30s | server.go |
| `mailWatcherPollInterval` | 3s | server.go |
| `mailWatcherSyncTimeout` | 90s | server.go |
| `appleAccountKeepAliveDefaultInterval` | 4min | icloud_client.go |
| `imapIdleReadTimeout` | 25min | imap_client.go |
| `appleHashcashMaxBits` | 24 | apple_auth_client.go |
| 2FA pending TTL | 10min | apple_auth_client.go |
| 默认 build 号 | `2622Build20` | icloud_validate.go |

---

## 3. 项目 B：hme-manager（Python 会话导入式）

### 3.1 架构与文件职责

```
web_app.py                  HTTP 服务（ThreadingHTTPServer）、路由分发、后台 worker 启动
hme.py                      HME 协议客户端（generate/reserve/update/deactivate/delete/list）+ 配置加载
session_import.py           cURL / HAR 解析与校验，写出 hme-config.json
icloud_web_session.py       ICloudSessionManager：会话状态、可用性检查、失败即禁用自动刷新
alias_creation_queue.py     持久化批量创建队列（SQLite）+ 限流退避 + 崩溃恢复
api_service.py              /v1/* 的业务函数与统一响应信封
mailbox_client.py           IMAP 只读客户端 + HTML 清洗（sanitize_email_html）
mailbox_sync.py             后台增量同步 + 本地 SQLite 邮件缓存 + 隐藏记录 + 长轮询 revision
verification_code.py        保守式验证码识别
share_links.py              可撤销只读分享链接（只存 token 的 SHA-256）
secure_storage.py           私有目录 0700 / 原子写 0600
static/                     前端 SPA（index.html + app.js + app.css）
```

设计原则写在 README 里：**纯 Python 标准库、零第三方依赖**；不接收 Apple ID、密码或 2FA。

### 3.2 会话导入：用人工登录替代协议登录

这是与项目 A 最本质的区别。`hme-manager` 不实现任何登录，而是让人在浏览器里登录后把**已授权的请求**复制出来。

**获取步骤**（README 第 4 节）：
1. 打开 iCloud+ → Hide My Email。
2. F12 → Network，找到包含 `list?clientBuildNumber` 的请求。
3. 对该请求 **Copy as cURL (bash)**，或导出含 request cookies 的 HAR。
4. 后台"会话与自动刷新 → 手动导入会话"粘贴提交。

**解析**（`session_import.py`）：

- `parse_import_text` 先尝试 `json.loads`；是 HAR（有 `log` 键）就走 `_parse_har`，否则走 `parse_hme_curl`。
- `_parse_hme_url` 做严格校验：
  - 必须 HTTPS，不得带用户名密码，端口只能是 443 或空；
  - host 必须匹配 `[a-z0-9](?:[a-z0-9-]*[a-z0-9])?-maildomainws\.icloud\.com(?:\.cn)?`
    → **不允许手写分区编号**，必须用浏览器实际请求的 Host（如 `p35-maildomainws.icloud.com`）。
  - 路径必须以 `/v2/hme/list` 或 `/v1/hme/` 开头。
- 从 URL query 提取 `dsid`、`clientId`、`clientBuildNumber`、`clientMasteringNumber`；缺任何一个直接报错。
- Cookie 从 `-b` / `--cookie` / `-H cookie:` 三种写法里提取（HAR 则从 `headers` 或 `cookies` 数组拼）。
- `_require_core_session_cookies` 强制要求三个核心 Cookie 同时存在：
  `X-APPLE-DS-WEB-SESSION-TOKEN`、`X-APPLE-WEBAUTH-USER`、`X-APPLE-WEBAUTH-TOKEN`。
- 区域默认值按 host 后缀自动选：`.icloud.com.cn` → `langCode=zh-cn`、`origin=https://www.icloud.com.cn`；否则 `zh-tw` + `https://www.icloud.com`。
- 落盘：`save_imported_session` 用 `atomic_write_text` 写 `hme-config.json`（`0600`，同目录 `0700`），另写一份不含 Cookie 的元数据到 `state/hme-session.json`。

**运行期会话管理**（`icloud_web_session.py`）：

- `ICloudSessionManager.get_client()` 每次从 `hme-config.json` 现读配置并构造 `HmeClient`；文件不存在或 Cookie 仍是占位符 `PASTE_AUTHORIZED_ICLOUD_WEB_COOKIE_HERE` 时抛错。
- `check()` 打一次 `list_settings()`（即 `/v2/hme/list`），成功就写 `session-state.json`：`sessionValid=true`、`lastRefreshAt`、`lastValidAt`、`expiresHint="apple-controlled"`、`needsReauth=false`；失败写 `sessionValid=false` + `needsReauth=true` + 脱敏后的 `lastError`。
- **自动刷新 worker**（`web_app.py`）：默认 `enabled`，间隔 600 秒（下限 300 秒），每 30 秒醒一次检查是否到点。`run_auto_refresh_once` 里如果错误文本含 `HTTP 401/403/421`，或 `check()` 返回 `needsReauth`，就调用 `disable_auto_refresh` **自动关闭自动刷新**并把原因写进配置——这是"失效时自动停用"的实现。

> 结论：`hme-manager` 的会话就是"一份 Cookie + 四个参数"，寿命完全由 Apple 决定，工具只负责发现失效并停手。

### 3.3 HME 客户端：请求怎么发

`hme.py` 的 `HmeClient`：

**URL 构造**（`_url`）——四个查询参数全部拼上：

```
https://{host}{path}?clientBuildNumber=..&clientMasteringNumber=..&clientId=..&dsid=..
```

**请求头**（`_headers`）——注意 `Content-Type` 是 `text/plain`：

```http
Accept: */*
Accept-Language: zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7
Content-Type: text/plain
Origin: https://www.icloud.com[.cn]
Referer: https://www.icloud.com[.cn]/
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/147.0.0.0 Safari/537.36
Cookie: <导入的 Cookie 原文>
```

**创建（核心）**——`create_alias` = `generate_alias` + `reserve_alias`：

```http
POST /v1/hme/generate?<四个参数>
{"langCode":"<配置的 langCode>"}
→ {"success":true,"result":{"hme":"xxxxx@icloud.com"}}
```

```http
POST /v1/hme/reserve?<四个参数>
{"hme":"xxxxx@icloud.com","label":"<标签>","note":"<备注>"}
→ {"success":true,"result":{"hme":{...}}}
```

**其他已封装的接口**：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/v2/hme/list` | 列出全部别名（也用于会话检查） |
| POST | `/v1/hme/updateMetaData` | 改标签/备注（`{"anonymousId","label","note"}`） |
| POST | `/v1/hme/deactivate` | 停用 |
| POST | `/v1/hme/activate` | 启用 |
| POST | `/v1/hme/delete` | 删除 |

**错误分类**（`_request` + `HmeError`）——这是后续队列退避的依据：

| 场景 | `HmeError` 字段 |
| --- | --- |
| HTTP >= 400 | `http_status`、`apple_code`（从 `errorCode` / `code` / `error.errorCode` 提取）、`retry_after`（从 `retryAfter` 提取）、`kind="http"` |
| `success != true` | 同上，`kind="provider"` |
| 网络异常 | `kind="network"` |
| 正文不是 JSON 对象 | 普通 `HmeError` |

`redact_sensitive_text` 会把 `X-APPLE-DS-WEB-SESSION-TOKEN`、`X-APPLE-WEBAUTH-TOKEN`、`X-APPLE-WEBAUTH-LOGIN`、`X-APPLE-WEBAUTH-VALIDATE`、`Cookie`、`Authorization` 的值替换成 `<已隐藏>`，覆盖 JSON、Python repr 和原始 header 三种形态——所有对外错误消息都过这一层。

### 3.4 批量创建队列（`alias_creation_queue.py`）

这是 `hme-manager` 最重的工程部分，解决的问题是"Apple 会限流，而 HTTP 请求不能挂着等 30 分钟"。

**定位**：文件头注释写得很清楚——**这是可靠性特性，不是配额绕过**。遇到 Apple 短期创建上限时 worker 就等待并稍后继续，不轮换 Cookie/账号/代理、不提并发。

#### 3.4.1 数据模型（SQLite，`HME_STATE_DIR/alias-creation.sqlite3`）

| 表 | 关键列 |
| --- | --- |
| `alias_create_jobs` | `id`、`request_id`（幂等键，唯一索引）、`account_dsid`、`base_label`、`note`、`requested`、`status`、`resume_status`、`next_attempt_at`、`cooldown_stage`、`pause_requested`、`cancel_requested`、`last_error_*` |
| `alias_create_items` | `(job_id, position)` 主键、`label`、`state`、`candidate_hme`、`hme`、`anonymous_id`、`attempts`、`reconcile_required`、`invalid_candidate_retries` |
| `alias_account_cooldowns` | `account_key` 主键、`next_attempt_at`、`stage` |

状态机：

```
job.status :  queued / running / waiting_rate_limit / waiting_retry / paused / needs_attention / completed / cancelled
item.state :  pending / generating / generated / reserving / created / cancelled / needs_attention
```

`ACTIVE_STATUSES = {queued, running, waiting_rate_limit, waiting_retry}`，`HOLD_STATUSES = {paused, needs_attention}`，`TERMINAL_STATUSES = {completed, cancelled}`。
同一时刻**只允许一个非终态 job**（`_enqueue_new_job` 里检查 `latest`）。

#### 3.4.2 入队与幂等

`POST /v1/alias-queue` → `AliasCreationQueue.enqueue(base_label, count, note, request_id)`：

- 校验：`base_label` 非空；`count` 是 1–99 的整数（`MAX_QUEUE_COUNT = 99`）；`base+序号` 不超过 160 字符；`note` 不超过 2000 字符。
- **幂等**：`request_id` 会写进唯一索引。同 `request_id` 重试时，如果参数完全一致就直接返回原 job；参数不一致抛 `IDEMPOTENCY_CONFLICT`。
- 入队时先取当前会话的 `dsid` 存进 `account_dsid`，**把队列绑定到账号**。
- 如果该账号正在冷却中，job 直接以 `waiting_rate_limit` + 冷却到期时间入队。
- 一次性插入全部 item，label 为 `f"{base}{index+1:02d}"`（即 `登录01`、`登录02`…）。

#### 3.4.3 单步执行 `_run_once_locked`

队列**故意一次只做一个** `generate`/`reserve` 对。单步流程：

1. `BEGIN IMMEDIATE` 拿写锁，读最新 job；状态不在 `{queued, waiting_rate_limit, waiting_retry}` 就直接返回。
2. 检查账号冷却：未到点 → 写 `waiting_rate_limit` 并返回。
3. 检查 `next_attempt_at` 未到 → 返回。
4. 处理控制位：`cancel_requested` 且当前项不需要核对 → `_cancel_remaining`；`pause_requested` → 置 `paused` 并保存 `resume_status`。
5. 取当前 item，置 `generating`，job 置 `running`，**提交事务**。
6. **在事务外**、持 `create_gate` 的情况下联系 Apple：
   - 校验会话 `dsid` 与 job 的 `account_dsid` 一致，不一致就 `ACCOUNT_CHANGED` 需要人工处理；
   - 没有候选地址 → `client.generate_alias()` → `_save_candidate`；
   - `reconcile_required`（上次结果不明）→ 先 `list_aliases()` 找候选；找到就算成功，找不到才继续 reserve；
   - 正常路径：`_mark_reserving` → `client.reserve_alias(candidate, label, note)` → `_finish_success`。
7. 成功后 `_notify_created(address)` 刷新邮件同步的别名白名单（`refresh_mailbox_aliases`，失败被吞掉，不影响已成功的创建）。

`_finish_success` 会把 `next_attempt_at` 设为 `now + SUCCESS_SPACING_SECONDS`（2 秒）作为成功节流，并清除该账号的冷却记录。

#### 3.4.4 错误处理矩阵 `_handle_hme_error`

这是整个队列最值得抄的一段：

| 判定函数 | 触发条件 | 处理 |
| --- | --- | --- |
| `_is_rate_limit_error` | `http_status == 429`，或 `apple_code == "-41015"`，或文本含 `http 429` / `-41015` / `reached the limit of addresses` / `rate limit` / `too many requests` / `暂时限制创建` / `达到地址创建上限` | `waiting_rate_limit`，指数退避 |
| `_is_session_error` | `http_status in {401,403,421}`，或文本含 `HTTP 401/403/421` | `needs_attention` + `SESSION_EXPIRED` |
| `_is_network_error` | `kind == "network"`，或文本含 `网络错误` / `timed out` / `timeout` / `连接` / `connection reset` | 无候选时 `waiting_retry` 60 秒；有候选时视为"结果不明" |
| `-41003` 且处于 `reserving` | Apple 判定候选地址无效/过期 | 第一次：丢弃候选，`pending` + 2 秒后重新 generate（`INVALID_CANDIDATE_RETRY`）；第二次：`needs_attention` |
| 其他 | — | `needs_attention`，错误码取 `apple_code` 或 `ICLOUD_ERROR` |

**退避算法**：

```
stage  = max(job.cooldown_stage, account_cooldown.stage) + 1
seconds = min(MAX_COOLDOWN_SECONDS, COOLDOWN_SECONDS * 2^(stage-1))
if error.retry_after is not None:
    seconds = max(seconds, ceil(retry_after))
```

默认 `COOLDOWN_SECONDS = 1800`（30 分钟）、`MAX_COOLDOWN_SECONDS = 7200`（120 分钟），可用 `HME_ALIAS_QUEUE_COOLDOWN_SECONDS` / `HME_ALIAS_QUEUE_MAX_COOLDOWN_SECONDS` 覆盖。冷却按 `account_key`（即 `dsid`）持久化到 `alias_account_cooldowns`，**所以暂停或取消当前批次后再提交新批次，也不能提前绕过等待**。

#### 3.4.5 "结果不明确"的处理（防重复创建的核心）

`reserve_result_uncertain` 为真的两种情况：
- 有候选地址且是网络错误；
- item 处于 `reserving` 且错误不是 `http`/`provider` 类型。

处理方式：**先核对，不重试**。调用 `list_aliases()` 精确查找该候选地址（`_find_candidate`）：
- 找到 → 判定成功，`_finish_success` 并通知；
- 找不到 → `needs_attention` + `reconcile_required=1` + `RESULT_UNCERTAIN`，提示"请刷新邮箱列表确认后再继续"。

服务重启时 `_recover_after_restart` 也走同一逻辑：如果重启瞬间 item 处于 `reserving`，标记 `SERVICE_RESTART_UNCERTAIN` + `reconcile_required=1`；否则把 `generated` 保留、其余回退 `pending`，job 回 `queued` 继续。

`resume(confirm_uncertain=True)` 是人工确认入口；若此时 `cancel_requested` 已置位，`_cancel_job_after_reconcile_miss` 会再核对一次候选是否真的创建了，避免漏记。

#### 3.4.6 单 worker 保证

- 进程内：`self._worker_lock`（`threading.Lock` 非阻塞获取），保证 `run_once` 串行。
- 进程间：`{state_path}.worker.lock` + `fcntl.flock(LOCK_EX | LOCK_NB)`，抢不到锁的进程每 2 秒重试，抢到锁的才执行 `_recover_after_restart` 并进入 `_worker_loop`。
- `_worker_loop` 每轮 `run_once`；返回 False 时按 `_next_wait_seconds()` 决定等待：`queued/running` → 0.5 秒；`waiting_*` → `min(30, next_attempt_at - now)`；其他 → 30 秒。
- 与手动创建、会话导入共享 `create_gate`（`ALIAS_CREATION_GATE`），避免同一会话被并发使用。
- 队列进行中时，手动 `POST /v1/aliases` 返回 `409 QUEUE_ACTIVE`；手动创建撞上限流会被 `record_manual_rate_limit` 记进同一套账号冷却。

### 3.5 收验证码：转发收件箱 IMAP + 本地缓存

README 把最关键的前提写得很直白：**HME 是转发别名，不是独立收件箱**。要读邮件必须接入 `forwardToEmail` 指向的真实收件箱（示例用 Gmail）。

#### 3.5.1 配置与连接

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `HME_IMAP_USERNAME` | 必填 | 转发目标地址 |
| `HME_IMAP_PASSWORD_FILE` | 必填 | 应用专用密码文件（Docker 默认 `/run/secrets/gmail-imap-app-password`） |
| `HME_IMAP_HOST` / `HME_IMAP_PORT` | `imap.gmail.com` / `993` | |
| `HME_IMAP_MAILBOX` | `INBOX` | 只读查询的邮箱 |
| `HME_IMAP_TIMEOUT_SECONDS` | 20 | 1–120 |
| `HME_IMAP_LOOKBACK_DAYS` | 90 | 首次扫描回看天数 |
| `HME_IMAP_MAX_RESULTS` | 50 | 1–100 |
| `HME_IMAP_MAX_MESSAGE_BYTES` | 2 MiB | 64 KiB–10 MiB |
| `HME_IMAP_MAX_TEXT_CHARS` | 200000 | 1000–1000000 |

`MailboxConfig.validated()` 对每一项都做范围校验，并对凭据做控制字符检查。连接用 `imaplib.IMAP4_SSL`，TLS 最低 1.2。

**认证回退**（`MailboxClient._connect`）：先 `AUTHENTICATE PLAIN`；失败时**必须重连**再降级到 `LOGIN`（IMAP 连接在认证失败后不能安全切换机制）；如果失败原因是连接数上限（`_is_connection_limit_error`）就直接报 `MailboxConnectionError`。打开邮箱固定 `readonly=True`。

**单连接复用**：`_session()` 上下文管理器持一把 `RLock`，进程内只维持一条只读连接；空闲超过 60 秒才做一次 `NOOP` 存活探测（因为国内到 Gmail 一次 NOOP 可能几秒）；任何协议/网络错误立即丢弃连接，下次请求重建。

#### 3.5.2 扫描策略（`scan_sync_batch`）

刻意**一次全邮箱 UID 搜索**，而不是每个别名搜一次：

1. `_read_uid_validity` 读 `UIDVALIDITY`。
2. `_search_sync_uids`：
   - 首次（游标为 `None`）：`UID SEARCH SINCE <now - lookback_days>`，并只取**最新窗口** `ordered_uids[-limit:]`；
   - 增量：`UID SEARCH UID <cursor+1>:*`。
3. 首次或还有积压 → 先 `_fetch_headers` 拉头部（`BODY.PEEK[HEADER.FIELDS (...)]<0.65536>`），在本地用 `_message_aliases` 比对白名单，只对命中的 UID 拉正文；
   已追平的单条增量 → 直接拉正文（省一次 Gmail 命令，这是延迟关键路径）。
4. `_fetch_messages` 用 `BODY.PEEK` 分段拉取，配合 `max_message_bytes` 限制内存。
5. 解析后每个命中别名产出一条记录，带 `body` / `bodyText` / `safeHtml` / `truncated`。

**别名匹配**（`_message_aliases`）覆盖 9 个收件人头：`To`、`Cc`、`Resent-To`、`Delivered-To`、`X-Original-To`、`X-Forwarded-To`、`Envelope-To`、`X-Envelope-To`、`Apparently-To`——转发场景下真正命中的通常是后面几个。

#### 3.5.3 本地缓存（`mailbox_sync.py` 的 `MailboxCache`）

`HME_STATE_DIR/mail-cache.sqlite3`（`0600`，WAL）：

| 表 | 用途 |
| --- | --- |
| `messages` | `(alias, uid)` 主键，存解析后的纯文本、清洗后的 HTML、元数据 |
| `sync_meta` | 键值：游标、`uid_validity`、`initialSyncComplete`、`revision`、`mailbox_namespace` |
| `hidden_messages` | `(mailbox_namespace, uid_validity, uid)` 主键，本地隐藏记录 |

- **邮箱代际（namespace + generation）**：`_mailbox_namespace` 由接收账号派生，`_new_mailbox_generation()` 生成代际串。所有写操作都要通过 `_require_generation_cas` 校验 `uid_validity` 与 generation，不匹配抛 `MailboxGenerationChanged`，前端提示刷新。UIDVALIDITY 变化时 `reset_for_uidvalidity` 清库重建——因为 **UID 在邮箱被替换后不稳定**。
- **revision 长轮询**：每次提交新邮件 `_increment_revision`。前端用 `GET /v1/mail/sync/wait?revision=n&timeout=25` 长轮询等版本变化（带 `X-API-Key` 头，不放 URL），另有 60 秒兜底轮询。
- **隐藏记录**：`hide_messages` 是原子批量操作，最多 200 条。它**只删本地 SQLite 中同 UID 的所有视图投影并写 tombstone，绝不向 IMAP 发 DELETE / MOVE / EXPUNGE**。tombstone 保留最长 366 天、总量上限 10000 条，UIDVALIDITY 或接收账号变化时清空。

#### 3.5.4 后台同步 worker

- `MailboxSyncService._worker_loop` 单线程，`run_sync_once` 是唯一入口。
- **IDLE**：Docker 镜像用 Python 3.14，`imaplib` 才有 `idle()`。`idle_wait` 检测不到可调用 `idle` 时返回 `None`，同步器退回有界 UID 轮询（`HME_MAIL_SYNC_POLL_SECONDS`，默认 5 秒）。
  > 代码注释解释了为什么不自己驱动 `imaplib` 的私有缓冲 socket：读超时会污染 `makefile` 对象，让 worker 陷入重连循环。
- 旧运行时每轮轮询前会 `refresh_selected_mailbox` 重新 `SELECT`（只读），避免 Gmail 长连接停留在陈旧的 `EXISTS` 视图。
- 别名白名单变化时用 `_alias_generation` 做 CAS：如果 IMAP 请求在飞的时候白名单变了，整批丢弃并标记需要重扫，防止陈旧数据被写回。

### 3.6 验证码识别（`verification_code.py`）

设计原则写在文件头：**宁漏不误**——绝不把日期、订单号、电话号码、URL 里的数字当成验证码。识别结果只在详情响应里即时计算，不写入缓存，也不会自动复制。

- 候选来源：`_NUMERIC_RE`（4–8 位数字）、`_GROUPED_SIX_RE`（`123 456` / `123-456`）、`_ALPHANUMERIC_RE`（4–12 位字母数字）。
- 上下文打分：`_STRONG_CONTEXT_RE` 命中"验证码/校验码/动态码/安全码/one-time/verification code/認証コード/인증 코드/รหัสยืนยัน/código de verificación/code de vérification"等强上下文会加权；`_NEGATIVE_CONTEXT_RE` 命中订单号、发票、追踪号等会减权。
- 排除规则：URL 内部或紧邻 URL 的数字（`_inside_or_near_url`）、长数字的一部分（`_part_of_long_number`）、看起来像日期的值（`_looks_like_calendar_value`）。
- 最多返回 3 个候选（`_MAX_CODES`），上下文距离窗口 72 字符（`_CONTEXT_DISTANCE`）。
- 另有 `extract_partner_codes` 专门识别 OpenAI 的 16 位合作伙伴代码（必须同时含字母和数字）。

### 3.7 对外 API 与安全边界

统一响应信封：

```json
{ "ok": true, "data": {}, "error": null,
  "meta": { "service": "hme-manager", "version": "1", "requestId": null } }
```

鉴权（`api_service.require_api_key`）：
- 默认所有 `/v1/*` 都要 `X-API-Key`，用 `hmac.compare_digest` 常量时间比较。
- `HME_AUTH_REQUIRED=0` 时（仅限可信 localhost/SSH 隧道）除隐藏邮件端点外可省略；但所有写请求仍必须带 `X-HME-Requested-With: hme-manager`，用来挡跨站简单请求。
- **隐藏邮件接口始终要求 `X-API-Key`**，即使免 Key 模式也不例外。

只读分享（`share_links.py` + `/share/v1/*`）：
- 生成时返回完整链接一次，随机令牌放在 URL fragment（`#...`）里，不会进服务器或 Cloudflare 的请求日志。
- 分享页立刻把它兑换成独立的随机短期会话令牌，放进 `HttpOnly` / `Secure` / `SameSite` Cookie，并从地址栏移除。
- 数据库只存令牌的 **SHA-256 摘要**，不存完整链接、后台 API Key、Gmail 密码或 iCloud 会话。
- 会话最长 7 天，且不超过分享链接本身的有效期。
- `/share/v1/*` 只提供只读能力，且服务端把所有邮件查询固定到令牌所属的那一个 HME 地址。

其它：`PRIVATE_API_BODY_MAX_BYTES = 64 KiB`、`SESSION_IMPORT_BODY_MAX_BYTES = 8 MiB`；静态 HTML 清洗（`sanitize_email_html`）移除脚本、表单、危险协议、附件、远程图片/字体/CSS URL，只保留 `http`/`https` 链接并强制新窗口打开。

---

## 4. 两者实现差异对照（逐项）

| 环节 | iCloud-Privacy-Mail | hme-manager |
| --- | --- | --- |
| **拿会话** | 自己跑 SRP + 2FA，面板输入 Apple ID/密码 | 人工在浏览器登录后导入 cURL/HAR |
| **支持的登录通道** | 旧接口（`icloud_web`）+ 新接口（`apple_account`） | 仅旧接口 |
| **会话区域处理** | 自动：解析 `domainToUse` / `accountCountryCode` 切 `.cn` / 国际站 | 自动：按导入 URL 的 host 后缀选 `langCode`/`origin`/`referer` |
| **Host 来源** | `validate` 返回的 `premiummailsettings.url` | 导入 URL 里的 `*-maildomainws.icloud.com[.cn]` |
| **会话寿命管理** | 新接口按 `timeOutInterval` 主动刷新 + 后台保活；旧接口长效 | 只做检测，失效即关闭自动刷新，等人重新导入 |
| **创建接口** | 新：`POST add` + `PUT add/complete`（+ 可选 `GET {id}.em`）；旧：`POST /v1/hme/generate` + `POST /v1/hme/reserve` | 仅 `POST /v1/hme/generate` + `POST /v1/hme/reserve` |
| **通道选择** | `auto` / 强制 `apple_account` / 强制 `icloud_web`，auto 时失败自动回落 | 无（只有一条路） |
| **同步已有邮箱** | `GET /v2/hme/list`，**固定走旧接口**；没有旧接口登录态会明确报错 | `GET /v2/hme/list`（也是会话检查用的接口） |
| **批量单位** | 一次请求：勾选的每个账号各创建 1 个，多账号并发 | 一个持久化队列：1–99 个，串行逐个创建 |
| **限流退避** | 命中限流 → 通道级冷却 2 分钟（内存态） | 命中限流 → 账号级持久化冷却 30 → 60 → 120 分钟指数退避 |
| **结果不确定时** | 刷新登录态后重试一次创建；同步失败时拒绝返回旧码 | 不重试，先 `list_aliases` 核对候选；核对不到就挂起等人工确认 |
| **并发闸门** | 账号级 channel 信号量 + 3 秒最小间隔 | 进程内 `worker_lock` + 进程间 `flock` + 全局 `create_gate` |
| **崩溃恢复** | 定时任务状态在内存，重启即丢 | 队列在 SQLite，重启后自动恢复；`reserving` 中断标为需核对 |
| **收码收件箱** | iCloud 自身邮箱 `imap.mail.me.com` + App 专用密码 | 转发目标收件箱（默认 Gmail）+ 应用专用密码 |
| **IMAP 拨号** | 自建三级兜底（硬编码 IP / DNS over TCP / 域名） | 标准 `imaplib.IMAP4_SSL`，靠系统 DNS |
| **增量策略** | 账号级 `UID SEARCH UID n:*` 游标，IDLE 触发 + 3 秒兜底轮询 | 全局 UID 游标，IDLE 触发 + 5 秒轮询兜底 |
| **邮件存储** | `state.json` 内嵌 `messages[]`（全量 JSON 重写） | 独立 SQLite 缓存 + revision 长轮询 |
| **别名匹配** | 全部 header 拼串后大小写无关包含匹配 | 9 个收件人头 + 精确地址解析 |
| **验证码识别** | 2 条正则（上下文 + 裸 6 位） | 上下文加权 + 负向排除 + 最多 3 个候选 |
| **对外接口** | 单邮箱 `mailbox_key` 取码；全局 `api_key` 自动取号 | 统一 `X-API-Key`；可生成只读分享链接 |
| **多租户** | 有（管理员 / 普通用户，`owner_id` 隔离） | 无 |
| **依赖** | Go 标准库 + embed | Python 标准库，零第三方 |

---

## 5. 端到端时序（以 iCloud-Privacy-Mail 新接口为例）

```
面板                     Server                    Apple                     IMAP
 │  输入 Apple ID/密码      │                         │                        │
 ├───────────────────────►│ StartAppleAccountManageLogin                    │
 │                        ├── warm portal ─────────►│                        │
 │                        ├── gs/ws/token ─────────►│ scnt                   │
 │                        ├── authorize/signin ────►│                        │
 │                        ├── verify/device/key/challenge ──►│              │
 │                        ├── federate ────────────►│                        │
 │                        ├── signin/init ─────────►│ salt/B/iteration       │
 │                        │   (本地 PBKDF2 + SRP-6a) │                        │
 │                        ├── signin/complete + X-Apple-HC ►│ 409 = 需要 2FA │
 │◄── needs_2fa, pending_id┤                         │                        │
 │  受信任设备弹码           │                         │                        │
 ├── 6 位验证码 ──────────►│ SubmitAppleAccountManage2FA                     │
 │                        ├── verify/trusteddevice/securitycode ──►│        │
 │                        ├── 2sv/trust ───────────►│                        │
 │                        ├── GET /account/manage ──►│ apiKey                 │
 │◄── 登录态已保存 ─────────┤ SaveICloudSessionForOwner (state.json)          │
 │                        │                         │                        │
 │  点"开始创建"            │                         │                        │
 ├───────────────────────►│ createMailboxesForOwnerWithChannels             │
 │                        ├── POST email/private/add ──►│ emailAddress       │
 │                        ├── PUT  add/complete ────►│ id/label/active        │
 │                        ├── GET  {id}.em ─────────►│ forwardToEmail         │
 │◄── 邮箱 + API 地址 ──────┤ AddMailboxForOwner + api_token                  │
 │                        │                         │                        │
 │  保存取码登录            │                         │                        │
 ├── 邮箱 + App 专用密码 ──►│ CheckICloudIMAPLogin ──────────────────────────►│ LOGIN/SELECT
 │◄── 取码登录正常 ─────────┤ withICloudIMAPLoginState                        │
 │                        │                         │                        │
 │  外部项目取码            │                         │                        │
 ├── GET .../code?key= ───►│ 本地缓存命中？ ── 否 ──► 快速补抓 ──────────────►│ UID SEARCH/FETCH
 │◄── {"code":"123456"} ───┤ extractOTP + SetMailboxLastCode                 │
```

---

## 6. 关键常量与阈值汇总

| 项目 | 项 | 值 |
| --- | --- | --- |
| Go | 创建最小间隔 | 3 秒 |
| Go | 通道限流冷却 | 2 分钟 |
| Go | 取码新鲜窗口 | 5 分钟 |
| Go | 取码快速补抓等待 | 600 ms（`wait_ms` 上限 30 秒） |
| Go | 后台同步轮询 | 3 秒 |
| Go | 新接口保活间隔 | 240 秒 |
| Go | IMAP IDLE 读超时 | 25 分钟 |
| Go | hashcash 上限 | 24 bit / 2^24 次 |
| Go | 2FA pending 有效期 | 10 分钟（进程内存） |
| Go | 默认 build 号 | `2622Build20` |
| Go | 旧接口限额口径 | 约 5 个/小时 |
| Go | 新接口限额口径 | 约 20 个/小时 |
| Py | 队列单批上限 | 99 |
| Py | 成功节流 | 2 秒 |
| Py | 限流首次冷却 | 1800 秒（30 分钟） |
| Py | 限流最长冷却 | 7200 秒（120 分钟） |
| Py | 网络错误重试 | 60 秒 |
| Py | `-41003` 重试 | 2 秒（只重试一次） |
| Py | 自动刷新间隔 | 600 秒（下限 300 秒） |
| Py | IMAP 轮询兜底 | 5 秒 |
| Py | 邮件聚合窗口 | 最近 72 小时 |
| Py | 分享会话最长 | 7 天 |
| Py | 隐藏记录保留 | 366 天 / 10000 条 |

---

## 7. 错误码与排障对照

### 7.1 iCloud-Privacy-Mail（`code` 字段）

| code | 含义 | 处理 |
| --- | --- | --- |
| `apple_credentials_missing` | 缺 Apple ID 或密码 | 补全后重试 |
| `apple_credentials_invalid` | SRP 返回 401 | 核对密码 |
| `apple_login_forbidden` | 403，密码错或账号被限制 | 换网络 / 等风控解除 |
| `apple_2fa_failed` | 验证码校验失败 | 确认码未过期、未重复提交 |
| `apple_hc_too_hard` / `apple_hc_failed` | hashcash 难度过高或生成失败 | 稍后重试 |
| `apple_domain_switch_failed` | 区域跳转后仍未完成 2FA | 重新发起登录 |
| `apple_login_pending_expired` | pending 超 10 分钟或进程重启 | 重新发起登录 |
| `apple_account_session_missing` | 没有新接口登录态 | 先做新接口登录 |
| `apple_account_api_key_missing` | 管理接口未返回 apiKey | 重新登录新接口 |
| `apple_account_auth_failed` | 419 或响应体含会话过期特征 | 自动刷新一次并重试；仍失败需重新登录 |
| `apple_account_hme_limit` | 命中创建上限 | 等通道冷却（2 分钟）后重试 |
| `apple_account_generate_empty` / `apple_account_create_empty` | 未返回候选 / 邮箱 | 看原始响应，可能风控 |
| `icloud_hme_unavailable` | 账号无 iCloud+ 或无 HME 权限 | 检查订阅 |
| `icloud_hme_limit` | 旧接口命中 `reached the limit of addresses` | 等冷却 |
| `mailbox_create_all_channels_failed` | 新接口 + 旧接口都失败 | 看消息里两段原始错误 |
| `mail_sync_failed` | 同步失败且未开 `allow_stale` | 检查取码登录，或显式开 `allow_stale` |
| `no_code` | 未收到验证码 | 带 `after` 参数继续轮询 |

调试开关：`IPM_DEBUG_APPLE_ACCOUNT=1`（新接口 / 管理接口）、`IPM_DEBUG_APPLE_AUTH=1`（登录链路）。日志只输出指纹，不输出明文。

### 7.2 hme-manager（`error.code`）

| code | 触发 |
| --- | --- |
| `RATE_LIMITED` | 429 或 `-41015`，队列进入冷却 |
| `SESSION_EXPIRED` | 401 / 403 / 421 |
| `NETWORK_RETRY` | 网络错误且尚无候选地址 |
| `INVALID_CANDIDATE_RETRY` | `-41003`，丢弃候选重新 generate |
| `RESULT_UNCERTAIN` | reserve 结果不明，需人工核对 |
| `SERVICE_RESTART_UNCERTAIN` | 重启时正处于 reserving |
| `ACCOUNT_CHANGED` | 当前会话 `dsid` 与队列绑定账号不一致 |
| `QUEUE_ACTIVE` | 已有非终态队列 |
| `IDEMPOTENCY_CONFLICT` | 同一 `requestId` 用于不同参数 |
| `ALIAS_INACTIVE` | 停用的隐藏邮箱不能生成分享链接 |
| `SHARE_LINK_INVALID` | 分享链接过期或已撤销 |
| `MAILBOX_GENERATION_CHANGED` | 邮箱代际变化，需刷新页面 |

排查顺序建议：`/health` → `/v1/session/status`（看 `sessionValid` / `needsReauth` / `lastError`）→ `/v1/alias-queue`（看 job 状态与 `lastErrorCode`）→ `/v1/mail/sync/status`（看同步游标与 `revision`）。

---

## 8. 复刻要点（如果要自己实现一套）

按依赖顺序，最小可用路径是：

1. **会话来源二选一**
   - 轻量路线（学 `hme-manager`）：让用户在浏览器登录后复制 `/v2/hme/list` 的 cURL，从 URL query 取 `dsid` / `clientId` / `clientBuildNumber` / `clientMasteringNumber`，从 `-b` 取 Cookie，并强制校验 `X-APPLE-DS-WEB-SESSION-TOKEN`、`X-APPLE-WEBAUTH-USER`、`X-APPLE-WEBAUTH-TOKEN` 三个 Cookie 都在。host 不要写死，用浏览器实际请求的那个。
   - 全自动路线（学 `iCloud-Privacy-Mail`）：实现 SRP-6a（注意 `s2k` / `s2k_fo` 两种派生）、`2sv/trust`、`accountLogin(extended_login=true)`，再打 `setup/ws/1/validate` 拿 `premiummailsettings` 地址和 HME 权限位。

2. **创建**：先 `POST /v1/hme/generate` 拿候选，再 `POST /v1/hme/reserve` 占用。两个请求都要带那四个 query 参数，`Content-Type: text/plain;charset=UTF-8`，`Origin` / `Referer` 用对应区域的 `www.icloud.com[.cn]`。

3. **创建结果落库**：邮箱地址做唯一键；`anonymousId` 后续改标签 / 停用 / 删除都要用；给每个邮箱生成独立随机 token 作为取码凭证。

4. **限流处理**：识别 `reached the limit of addresses` / `-41015` / HTTP 429，按账号做**持久化**冷却，指数退避（30 → 60 → 120 分钟），不要靠重启绕过。

5. **结果不确定**：任何超时 / 畸形响应之后**不要直接重试**，先 `GET /v2/hme/list` 精确查找候选地址；找不到再决定是重试还是挂起。

6. **收码**：接转发目标收件箱的 IMAP。用 `BODY.PEEK` 保持只读；用 `UID SEARCH UID n:*` 做增量；匹配时把 `Delivered-To` / `X-Original-To` / `X-Forwarded-To` 一起纳入；同一条已发放的验证码要记录 message id 防止重复返回。

7. **取码接口**：强制 `after` 参数；本地未命中时短等待（几百毫秒）触发一次补抓；同步失败时**默认拒绝返回旧码**，只在显式 `allow_stale` 时才回退。

8. **安全**：Cookie / App 密码落盘一律 `0600`；日志与错误消息统一走脱敏函数；对外错误消息里绝不出现完整 Cookie、`scnt`、apiKey、验证码。

---

## 9. 已知限制与风险

- Apple 网页协议随时可能变；风控、地区端点或参数一变就需要重新适配。
- 2FA pending 状态只存在进程内存，服务重启后必须重新登录（Go 项目）。
- iCloud 登录态会过期；过期后需重新保存。
- 新接口管理态是短期窗口，必须持续保活；保活失败要重新登录，不能只靠 `/v2/jslogs` 判断存活。
- 旧接口创建额度约 5 个/小时，新接口约 20 个/小时；任何"退避队列"都只是排队，不突破 Apple 配额。
- `hme-manager` 的 IMAP 在 Python 3.14 以下没有 `idle()`，只能退回轮询，实时性下降。
- 隐藏邮件只删本地缓存，不删 Gmail / iCloud 原邮件；超过保留期或容量上限后，完整重扫可能让邮件重新出现。
- 邮箱 API token 会因"用本地 state.json 覆盖服务器"而失效——迁移时必须按邮箱地址做安全合并，保留服务器侧 token。

---

## 10. 参考文件索引

### iCloud-Privacy-Mail

| 主题 | 文件 |
| --- | --- |
| 登录链路（SRP / 2FA / 区域跳转） | `internal/app/apple_auth_client.go`、`internal/app/apple_srp.go` |
| 创建、保活、指纹伪造、Cookie 合并 | `internal/app/icloud_client.go` |
| 登录态校验（`setup/ws/1/validate`） | `internal/app/icloud_validate.go` |
| 创建编排、限流冷却、取码 API、后台同步器 | `internal/app/server.go` |
| 定时批量创建 | `internal/app/scheduler.go` |
| IMAP 拨号兜底、IDLE、UID 增量 | `internal/app/imap_client.go` |
| 数据结构与状态文件格式 | `internal/app/models.go`、`internal/app/store.go` |
| 使用说明 / 变更记录 / 设计记录 | `README.md`、`更新日志.md`、`iCloud隐私邮箱取码平台接入设计.md` |

### hme-manager

| 主题 | 文件 |
| --- | --- |
| HME 协议客户端与错误分类 | `hme.py` |
| cURL / HAR 会话导入与校验 | `session_import.py` |
| 会话状态与自动刷新 | `icloud_web_session.py`、`web_app.py` |
| 持久化批量创建队列与退避 | `alias_creation_queue.py` |
| IMAP 客户端与 HTML 清洗 | `mailbox_client.py` |
| 后台同步、SQLite 缓存、隐藏记录 | `mailbox_sync.py` |
| 验证码识别 | `verification_code.py` |
| 分享链接 | `share_links.py` |
| 使用说明 | `README.md`、`SECURITY.md` |

---

## 附录：旧接口请求速查（两个项目通用）

```http
# 列出全部别名（也用于会话存活检查）
GET  https://{host}/v2/hme/list?clientBuildNumber=..&clientMasteringNumber=..&clientId=..&dsid=..

# 生成候选地址
POST https://{host}/v1/hme/generate?<同上四参数>
Content-Type: text/plain;charset=UTF-8
{"langCode":"zh-cn"}

# 占用候选地址（真正创建）
POST https://{host}/v1/hme/reserve?<同上四参数>
{"hme":"xxxxx@icloud.com","label":"<标签>","note":"<备注>"}

# 改标签/备注
POST https://{host}/v1/hme/updateMetaData?<同上四参数>
{"anonymousId":"...","label":"...","note":"..."}

# 停用 / 启用 / 删除
POST https://{host}/v1/hme/deactivate?<同上四参数>   {"anonymousId":"..."}
POST https://{host}/v1/hme/activate?<同上四参数>     {"anonymousId":"..."}
POST https://{host}/v1/hme/delete?<同上四参数>       {"anonymousId":"..."}
```

统一响应信封：

```json
{
  "success": true,
  "result": {},
  "error": { "errorCode": "", "errorMessage": "" },
  "timestamp": 0
}
```

`{host}` 取浏览器实际请求的 `*-maildomainws.icloud.com` 或 `*-maildomainws.icloud.com.cn`（Go 项目则来自 `validate` 返回的 `premiummailsettings.url`）；Cookie 必须包含 `X-APPLE-DS-WEB-SESSION-TOKEN`、`X-APPLE-WEBAUTH-USER`、`X-APPLE-WEBAUTH-TOKEN`。
