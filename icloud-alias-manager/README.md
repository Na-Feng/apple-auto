# iCloud 别名邮箱多账号智能自动化管理平台 (iCloud Alias Hub)

基于 Apple iCloud+ 隐私邮箱（Hide My Email, 简称 HME）协议构建的多账号批量管理与自动化创建系统。

---

## 🌟 核心特性

- **⚡ 免浏览器纯协议全自动导入（核心推荐）**：
  - **零浏览器操作**：直接粘贴账号凭证（`账号----密码----取码URL`），支持单行或数百行批量导入！
  - **全自动协议登录**：基于 Apple SRP-6a 2048 位密码学纯 Python 协商握手，无需安装外部复杂编译组件。
  - **智能 2FA 捕获**：系统触发设备/短信 2FA 推送后，自动高频轮询取码接口捕获最新 6 位验证码，向 Apple 提交并建立长效信任会话。
- **多账号集中管理**：支持同时接入任意数量的 iCloud 账号，自动追踪各账号别名总数、默认转发收件箱与凭证有效性。
- **免密零风控导入（备用通道）**：亦支持直接通过浏览器已授权的 cURL / HAR 会话一键导入。
- **智能调度策略**：
  - **轮询均衡 (Round-Robin)**：循环使用各可用账号创建别名，均匀分摊创建配额。
  - **顺序打满 (Fill-First)**：优先使用单个账号直至达到限额，自动切换至下一个账号。
  - **指定账号 (Specific)**：在指定的单个账号上定向创建。
- **智能风控退避与自动冷却**：
  - 自动识别 Apple 限流响应（HTTP 429、`-41015`、创建上限等），触发阶梯式指数冷却（30min &rarr; 60min &rarr; 120min）。
  - **自动故障转移**：遇到账号限流时，任务不会中断，系统立即自动将剩余创建项调度至下一个可用正常账号！
- **意图防重与崩溃恢复 (Intent-First & Reconcile)**：
  - 网络抖动或超时中断时，先落盘意图，并主动核对 iCloud 远端别名列表，绝不重复生成造成宝贵的配额浪费。
- **TLS 指纹防探测**：
  - 支持 `curl_cffi` 完整模拟 Chrome / Safari 浏览器 TLS JA3 指纹，并支持标准库优雅降级回退。
- **现代化可视化控制台 (Web Dashboard)**：
  - 响应式控制台，涵盖仪表盘、账号管理、批量任务监控、别名资产库与实时事件流水。
- **标准化 REST API**：
  - 提供开箱即用的 HTTP API，供爬虫、自动化注册脚本、批量测试机器人远程调用。
- **灵活导出**：
  - 支持将别名邮箱一键导出为纯文本 TXT（一行一个邮箱，方便脚本取用）、CSV 表格或 JSON。

---

## 🚀 快速启动

### 环境要求
- **Python 3.10+** (系统已内置支持，零强制第三方依赖)
- 可选依赖：`curl_cffi`（推荐安装，用于浏览器 TLS 指纹模拟）

### 启动服务

#### 方式一：Windows 双击启动
直接双击运行目录下的 `start.bat`。

#### 方式二：命令行启动
```bash
python main.py
```

启动后，访问控制台：
- 本机访问：[http://localhost:8080](http://localhost:8080)
- 局域网访问：`http://<本机IP>:8080`

自定义端口与参数：
```bash
python main.py --port 9000 --no-browser
```

## 📖 账号导入方式

### 方式一：⚡ 批量协议免浏览器一键导入（推荐）
系统支持直接输入账号列表，全自动执行 Apple SRP-6a 协议握手、自动调用取码链接获取 6 位 2FA 验证码，换取长效登录会话：
- **格式**：`账号----密码----取码URL`
- **示例**：
  ```text
  your_account@gmail.com----YourPassword123----https://devices.example.com/api/getcode?id=123456
  ```
- **使用方法**：
  1. 打开控制台 [http://localhost:8080](http://localhost:8080)
  2. 点击顶部快捷入口 **「⚡ 批量协议格式导入」** 或账号管理界面的 **「⚡ 批量账号导入」** 按钮
  3. 粘贴上述格式文本（支持多行批量输入），点击 **「开始全自动协议登录与导入」** 即可实时观察各步骤进度！

### 方式二：📥 浏览器已授权 cURL 导入（备用通道）
1. 在电脑浏览器登录 [icloud.com](https://www.icloud.com) 并进入 **隐藏我的电子邮件** 页面。
2. 按 `F12` 打开开发者工具，在网络面板筛选 `list` 或 `hme`。
3. 右键该请求 &rarr; **Copy &rarr; Copy as cURL (bash)**。
4. 在控制台选择 **「导入 cURL」** 粘贴保存即可。

---

## 🛠️ 项目目录结构

```
icloud-alias-manager/
├── main.py                     # 程序总入口
├── config.py                   # 配置定义
├── start.bat                   # Windows 一键启动脚本
├── requirements.txt            # 依赖声明
├── README.md                   # 详细使用指南
├── core/
│   ├── __init__.py
│   ├── db.py                   # SQLite 数据存储 (WAL 模式，ACID 事务)
│   ├── models.py               # 数据结构定义
│   ├── protocol_login.py       # 纯 Python Apple SRP-6a 协议登录与 2FA 取码
│   ├── curl_parser.py          # cURL / HAR / Cookie 解析与校验
│   ├── client_icloud_web.py    # iCloud Web 官方端点通信客户端
│   ├── client_apple_account.py # Apple Account 管理通道客户端
│   ├── scheduler.py            # 多账号调度器、限流退避、意图核对引擎
│   └── logger.py               # 敏感信息脱敏日志记录器
├── server/
│   ├── __init__.py
│   ├── app.py                  # ThreadingHTTPServer 与静态资源路由
│   └── api_handlers.py         # RESTful 业务接口分发
└── web/
    ├── index.html              # 现代化单页应用 (SPA)
    ├── app.js                  # 前端交互与实时轮询
    └── style.css               # Apple 风格精美界面样式
```

---

## 🔌 REST API 接口文档

### 1. 批量免浏览器协议导入
```http
POST /api/accounts/protocol-import
Content-Type: application/json

{
  "text": "your_account@gmail.com----YourPassword123----https://devices.example.com/api/getcode?id=123456"
}
```
**查询导入进度**：
```http
GET /api/accounts/protocol-import/status?job_id=xxxx
```

### 2. 账号管理接口

#### 获取所有账号
```http
GET /api/accounts
```

#### 导入账号 (支持 cURL 或手动参数)
```http
POST /api/accounts/import
Content-Type: application/json

{
  "name": "我的账号1",
  "curl": "curl 'https://p68-maildomainws.icloud.com/v2/hme/list?...' -H 'Cookie: ...'"
}
```

#### 连通性测试
```http
POST /api/accounts/{account_id}/check
```

#### 同步 iCloud 远端已有别名
```http
POST /api/accounts/{account_id}/sync
```

#### 手动解除限流冷却
```http
POST /api/accounts/{account_id}/clear-cooldown
```

---

### 2. 别名创建与操作接口

#### 单次创建别名（外部机器人/脚本推荐调用）
```http
POST /api/aliases/create
Content-Type: application/json

{
  "account_id": "",       // 留空则系统自动按策略挑选当前最佳账号
  "label": "用户注册01",   // 标签
  "note": "测试脚本使用"   // 备注
}
```

响应示例：
```json
{
  "ok": true,
  "data": {
    "id": "a1b2c3d4e5f6",
    "email": "mysterious.sunflower.0x@icloud.com",
    "anonymous_id": "...",
    "label": "用户注册01",
    "forward_to": "my_real_email@gmail.com",
    "created_at": 1774000000
  }
}
```

#### 启动批量创建任务
```http
POST /api/tasks/start
Content-Type: application/json

{
  "count": 20,                  // 批量创建个数
  "strategy": "round_robin",    // 调度策略: "round_robin" | "fill_first" | "specific"
  "target_account_id": "",      // 指定策略时的账号ID
  "base_label": "VIP",          // 标签前缀 (自动生成 VIP01, VIP02...)
  "note": "批量生成批次A"
}
```

#### 查询任务执行进度
```http
GET /api/tasks/current
```

#### 暂停 / 恢复 / 取消任务
```http
POST /api/tasks/pause
POST /api/tasks/resume
POST /api/tasks/cancel
```

---

### 3. 数据导出接口

```http
GET /api/export?format=txt&account_id=
GET /api/export?format=csv&account_id=
GET /api/export?format=json&account_id=
```
- `format=txt`：直接返回换行符分隔的纯邮箱列表，可供自动化直接遍历。
- `format=csv`：返回标准 CSV 格式表格，支持 Excel 直接打开。
