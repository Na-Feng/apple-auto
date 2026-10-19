# apple-auto: iCloud 隐私别名邮箱多账号智能自动化管理平台

基于 Apple 官方底层协议构建的 iCloud+ 隐私邮箱（Hide My Email, 简称 HME）多账号管理与批量自动化生成系统。

---

## 🌟 核心特性

- **⚡ 免浏览器纯协议全自动导入（核心特色）**：
  - **零浏览器操作**：直接粘贴账号凭证（`账号----密码----取码URL`），支持单行或数百行批量导入！
  - **全自动协议登录**：基于 Apple SRP-6a 2048 位密码学纯 Python 协商握手，无需依赖浏览器或外部复杂二进制组件。
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
- **现代化可视化控制台 (Web Dashboard)**：
  - 响应式控制台，涵盖仪表盘、账号管理、批量任务监控、别名资产库与实时事件流水。
- **标准化 REST API**：
  - 提供开箱即用的 HTTP API，供爬虫、自动化注册脚本、批量测试机器人远程调用。
- **灵活导出**：
  - 支持将别名邮箱一键导出为纯文本 TXT（一行一个邮箱，方便脚本取用）、CSV 表格或 JSON。

---

## 🚀 快速启动

### 环境要求
- **Python 3.10+** (系统标准库即可运行，零强制依赖)
- 可选依赖：`curl_cffi`（推荐安装，用于浏览器 TLS 指纹模拟）

### 启动服务

#### 方式一：Windows 双击启动
进入 `icloud-alias-manager` 目录，直接双击运行 `start.bat`。

#### 方式二：命令行启动
```bash
cd icloud-alias-manager
python main.py
```

启动后，访问控制台：
- 本机访问：[http://localhost:8080](http://localhost:8080)
- 局域网访问：`http://<本机IP>:8080`

自定义端口与参数：
```bash
python main.py --port 9000 --no-browser
```

---

## 📖 账号导入与使用说明

### 方式一：⚡ 批量协议免浏览器一键导入（推荐）
系统支持直接输入账号列表，全自动执行 Apple SRP-6a 协议握手、自动调用取码链接获取 6 位 2FA 验证码，换取长效登录会话：
- **格式**：`账号----密码----取码URL`
- **示例**：
  ```text
  your_account@gmail.com----YourPassword123----https://devices.example.com/api/getcode?id=123456
  account2@gmail.com----Password456----https://devices.example.com/api/getcode?id=654321
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
apple-auto/
├── README.md                           # 平台综合主文档
├── .gitignore                          # Git 忽略配置（防泄露数据库与凭据）
├── iCloud隐私邮箱创建实现文档.md       # Apple iCloud 隐私邮箱实现调研文档
├── 双通道隐私邮箱平台技术方案.md       # 架构设计与双通道技术规范方案
└── icloud-alias-manager/               # 核心管理平台主程序
    ├── main.py                         # 服务入口
    ├── config.py                       # 服务配置
    ├── start.bat                       # Windows 一键启动脚本
    ├── requirements.txt                # 依赖清单
    ├── README.md                       # 模块详尽文档
    ├── core/                           # 核心业务逻辑
    │   ├── protocol_login.py           # 纯 Python Apple SRP-6a 协议登录与 2FA 取码
    │   ├── client_icloud_web.py        # iCloud Web 官方端点通信客户端
    │   ├── client_apple_account.py     # Apple Account 管理通道客户端
    │   ├── scheduler.py                # 多账号智能调度、限流退避与故障转移
    │   ├── db.py                       # SQLite 数据层 (WAL 模式)
    │   ├── curl_parser.py              # cURL/HAR 会话解析器
    │   ├── models.py                   # 数据模型定义
    │   └── logger.py                   # 脱敏日志组件
    ├── server/                         # 服务端
    │   ├── app.py                      # HTTP 服务与静态资源路由
    │   └── api_handlers.py             # RESTful API 路由处理
    └── web/                            # 现代化单页控制台前端
        ├── index.html                  # 控制台 UI
        ├── app.js                      # 前端交互与实时任务监控
        └── style.css                   # Apple 风格响应式样式
```

---

## 🔌 REST API 快速调用

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

### 2. 发起批量创建任务
```http
POST /api/tasks/start
Content-Type: application/json

{
  "count": 50,
  "strategy": "round_robin",
  "base_label": "BotUser",
  "note": "批量注册用途"
}
```

### 3. 获取并导出别名
- 获取所有别名：`GET /api/aliases?limit=100`
- 导出纯文本 (一行一个)：`GET /api/export?format=txt`
- 导出 CSV 格式：`GET /api/export?format=csv`

---

## 📄 开源许可证

本项目基于 MIT 协议开源。
