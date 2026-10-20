// iCloud Alias Manager - Frontend Application Logic

const app = {
  currentTab: "dashboard",
  logFilter: "",
  logsCache: [],
  accounts: [],
  aliases: [],
  aliasTotal: 0,
  aliasPage: 0,
  aliasLimit: 20,
  activeJob: null,
  pollTimer: null,
  logTimer: null,

  init() {
    this.setupTabs();
    this.loadStats();
    this.loadAccounts();
    this.loadLogs();
    this.startJobPolling();
    this.startLogPolling();
  },

  // ==========================
  // API HELPERS
  // ==========================

  async apiGet(path) {
    try {
      const resp = await fetch(path);
      return await resp.json();
    } catch (e) {
      console.error(`GET ${path} failed:`, e);
      return { ok: false, error: e.message };
    }
  },

  async apiPost(path, body = {}) {
    try {
      const resp = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      return await resp.json();
    } catch (e) {
      console.error(`POST ${path} failed:`, e);
      return { ok: false, error: e.message };
    }
  },

  async apiDelete(path) {
    try {
      const resp = await fetch(path, { method: "DELETE" });
      return await resp.json();
    } catch (e) {
      console.error(`DELETE ${path} failed:`, e);
      return { ok: false, error: e.message };
    }
  },

  // ==========================
  // NAVIGATION & TABS
  // ==========================

  setupTabs() {
    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tab = btn.getAttribute("data-tab");
        this.switchTab(tab);
      });
    });
  },

  switchTab(tabName) {
    this.currentTab = tabName;
    document.querySelectorAll(".nav-item").forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-tab") === tabName);
    });

    document.querySelectorAll(".tab-view").forEach((v) => {
      v.classList.toggle("active", v.id === `view-${tabName}`);
    });

    const titles = {
      dashboard: "系统仪表盘",
      accounts: "iCloud 账号管理",
      proxy: "Resin 智能代理中枢",
      batch: "批量自动创建",
      aliases: "别名邮箱资产库",
      logs: "系统实时日志",
    };
    document.getElementById("page-title").textContent = titles[tabName] || "控制台";

    this.refreshCurrentTab();
  },

  refreshCurrentTab() {
    if (this.currentTab === "dashboard") {
      this.loadStats();
      this.checkCurrentJob();
    } else if (this.currentTab === "accounts") {
      this.loadAccounts();
    } else if (this.currentTab === "proxy") {
      this.loadProxyHub();
    } else if (this.currentTab === "batch") {
      this.loadAccounts();
      this.checkCurrentJob();
      this.loadAutopilotInfo();
    } else if (this.currentTab === "aliases") {
      this.loadAliases();
    } else if (this.currentTab === "logs") {
      this.loadLogs();
    }
  },

  // ==========================
  // DASHBOARD STATS
  // ==========================

  async loadStats() {
    const res = await this.apiGet("/api/stats");
    if (res.ok && res.data) {
      document.getElementById("stat-total-accounts").textContent = res.data.total_accounts;
      document.getElementById("stat-active-accounts").textContent = res.data.active_accounts;
      document.getElementById("stat-cooling-accounts").textContent = res.data.cooling_down_accounts;
      document.getElementById("stat-total-aliases").textContent = res.data.total_aliases;
    }
  },

  // ==========================
  // ACCOUNT MANAGEMENT
  // ==========================

  async loadAccounts() {
    const res = await this.apiGet("/api/accounts");
    if (!res.ok) {
      this.showToast(res.error || "获取账号列表失败", "error");
      return;
    }
    this.accounts = res.data || [];
    this.renderAccountsTable();
    this.populateAccountSelects();
  },

  renderAccountsTable() {
    const tbody = document.getElementById("accounts-table-body");
    if (this.accounts.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="text-center text-muted" style="padding: 30px;">暂无 iCloud 账号，请点击右上角导入</td></tr>`;
      return;
    }

    tbody.innerHTML = this.accounts.map((acc) => {
      let statusBadge = `<span class="badge badge-success">🟢 正常</span>`;
      let cooldownText = `<span class="text-muted">-</span>`;

      let channelBadge = `<span class="badge badge-primary">${acc.channel}</span>`;
      if (acc.channel === "dual") {
        channelBadge = `<span class="badge badge-success" style="background: linear-gradient(135deg, #059669 0%, #2563eb 100%); color:#fff; font-weight:600;">✨ 双通道</span>`;
        if (acc.apple_auth_expired) {
          if (acc.icloud_cooldown_remaining > 0) {
            statusBadge = `<span class="badge badge-warning">⏳ 冷却中</span>`;
            cooldownText = `<span style="color:#d97706; font-size:11px; font-weight:500;">⚡限流(${acc.icloud_cooldown_remaining}s) · 🍎凭证过期</span>`;
          } else {
            statusBadge = `<span class="badge badge-info">⚡ 仅iCloud可用</span>`;
            cooldownText = `<span style="color:#e11d48; font-size:11px; font-weight:500;">🍎 凭据失效 (iCloud正常)</span>`;
          }
        } else if (acc.apple_cooldown_remaining > 0 && acc.icloud_cooldown_remaining > 0) {
          statusBadge = `<span class="badge badge-danger">⏳ 双通道均限流</span>`;
          cooldownText = `<span class="badge badge-danger">官方限流中 (🍎${acc.apple_cooldown_remaining}s / ⚡${acc.icloud_cooldown_remaining}s)</span>`;
        } else if (acc.apple_cooldown_remaining > 0) {
          statusBadge = `<span class="badge badge-info">⚡ 仅iCloud可用</span>`;
          cooldownText = `<span style="color:#d97706; font-size:11px; font-weight:500;">🍎官方频控(${acc.apple_cooldown_remaining}s)</span>`;
        } else if (acc.icloud_cooldown_remaining > 0) {
          statusBadge = `<span class="badge badge-info">🍎 仅Apple可用</span>`;
          cooldownText = `<span style="color:#d97706; font-size:11px; font-weight:500;">⚡官方频控(${acc.icloud_cooldown_remaining}s)</span>`;
        } else {
          cooldownText = `<span style="color:#059669; font-weight:600; font-size:11px;">🟢 额度充沛 (~25/h)</span>`;
        }
      } else if (acc.channel === "apple_account") {
        channelBadge = `<span class="badge badge-warning" style="background:#ea580c; color:#fff;">🍎 Apple通道</span>`;
        if (acc.is_cooling_down) {
          statusBadge = `<span class="badge badge-warning">⏳ 冷却中</span>`;
          cooldownText = `<span class="badge badge-warning">剩余 ${acc.cooldown_remaining_seconds}s</span>`;
        }
      } else if (acc.is_cooling_down) {
        statusBadge = `<span class="badge badge-warning">⏳ 冷却中</span>`;
        cooldownText = `<span class="badge badge-warning">剩余 ${acc.cooldown_remaining_seconds}s</span>`;
      } else if (acc.status === "need_login") {
        statusBadge = `<span class="badge badge-danger">🔴 凭证失效</span>`;
        cooldownText = `<span class="text-danger">${acc.last_error || "需重新导入"}</span>`;
      }

      // Proxy display
      let proxyHtml = `<span class="badge" style="cursor:pointer; background:#f1f5f9; color:#64748b;" onclick="app.openProxyModal('${acc.id}')" title="点击配置代理">🌐 直连 (未配代理)</span>`;
      if (acc.proxy_url) {
        let displayProxy = acc.proxy_url.length > 20 ? acc.proxy_url.substring(0, 18) + "..." : acc.proxy_url;
        proxyHtml = `<span class="badge badge-success" style="cursor:pointer;" onclick="app.openProxyModal('${acc.id}')" title="${this.escapeHtml(acc.proxy_url)}">🌐 ${this.escapeHtml(displayProxy)}</span>`;
      }

      // 750 Target & Progress
      const curAliases = acc.current_total_aliases || Math.max(acc.alias_count || 0, acc.local_alias_count || 0);
      const target = acc.target_alias_count || 750;
      const pct = Math.min(100, Math.floor(curAliases / target * 100));
      const progHtml = `
        <div style="min-width: 130px;">
          <div style="display:flex; justify-content:space-between; font-size:11px; margin-bottom:2px;">
            <strong>${curAliases} / ${target}</strong>
            <span style="color:#2563eb; font-weight:600;">${pct}%</span>
          </div>
          <div style="height:6px; background:#e2e8f0; border-radius:3px; overflow:hidden;">
            <div style="height:100%; width:${pct}%; background:linear-gradient(90deg, #2563eb, #10b981); border-radius:3px;"></div>
          </div>
        </div>
      `;

      // Autopilot status & quick action
      let apStatusBadge = `<span class="badge" style="background:#f1f5f9; color:#64748b;">💤 空闲</span>`;
      let apActionBtn = `<button class="btn btn-sm btn-gradient" onclick="app.startAccountAutopilot('${acc.id}')" title="启动该账号专属 750 托管">🎯 750托管</button>`;

      if (acc.autopilot_status === "running") {
        apStatusBadge = `<span class="badge badge-success">🤖 托管中</span>`;
        apActionBtn = `<button class="btn btn-sm btn-warning" onclick="app.pauseAccountAutopilot('${acc.id}')">暂停</button>`;
      } else if (acc.autopilot_status === "waiting_cooldown") {
        const rem = acc.autopilot_remaining_seconds || 0;
        apStatusBadge = `<span class="badge badge-warning" title="${this.escapeHtml(acc.autopilot_last_error || '')}">⏳ 休眠(${rem}s)</span>`;
        apActionBtn = `<button class="btn btn-sm btn-secondary" onclick="app.stopAccountAutopilot('${acc.id}')">停止</button>`;
      } else if (acc.autopilot_status === "completed") {
        apStatusBadge = `<span class="badge badge-success">🎉 750满额</span>`;
        apActionBtn = `<span class="text-success" style="font-size:12px; font-weight:600;">已满额</span>`;
      } else if (acc.autopilot_status === "paused") {
        apStatusBadge = `<span class="badge badge-secondary">⏸️ 暂停中</span>`;
        apActionBtn = `<button class="btn btn-sm btn-primary" onclick="app.resumeAccountAutopilot('${acc.id}')">继续</button>`;
      }

      return `
        <tr>
          <td>
            <strong>${this.escapeHtml(acc.name)}</strong><br>
            <span class="text-muted" style="font-size:11px;">${this.escapeHtml(acc.apple_id || acc.selected_forward_to || "-")}</span>
          </td>
          <td>
            <div style="font-size:11px;">
              ${channelBadge}<br>
              <code>${this.escapeHtml(acc.host)}</code>
            </div>
          </td>
          <td>${proxyHtml}</td>
          <td>${progHtml}</td>
          <td>${apStatusBadge}</td>
          <td>${cooldownText}</td>
          <td>
            <div style="display:flex; gap:5px; flex-wrap:wrap;">
              ${apActionBtn}
              <button class="btn btn-sm btn-secondary" onclick="app.openProxyModal('${acc.id}')" title="配置代理与目标">⚙️ 代理</button>
              <button class="btn btn-sm btn-secondary" onclick="app.syncAccount('${acc.id}')" title="同步远端已存在别名">🔄 同步</button>
              <button class="btn btn-sm btn-danger" onclick="app.deleteAccount('${acc.id}', '${this.escapeHtml(acc.name)}')" title="删除账号">🗑️</button>
            </div>
          </td>
        </tr>
      `;
    }).join("");
  },

  populateAccountSelects() {
    // Populate batch strategy target account dropdown
    const batchSel = document.getElementById("batch-target-account");
    if (batchSel) {
      batchSel.innerHTML = this.accounts.map(a => `<option value="${a.id}">${this.escapeHtml(a.name)} (${a.is_cooling_down ? '冷却中' : a.status})</option>`).join("");
    }

    // Populate filter in aliases view
    const filterSel = document.getElementById("alias-account-filter");
    if (filterSel) {
      const current = filterSel.value;
      filterSel.innerHTML = `<option value="">全部账号 (${this.accounts.length})</option>` +
        this.accounts.map(a => `<option value="${a.id}">${this.escapeHtml(a.name)}</option>`).join("");
      filterSel.value = current;
    }

    // Populate single create modal
    const singleSel = document.getElementById("single-create-account");
    if (singleSel) {
      singleSel.innerHTML = `<option value="">自动挑选可用账号 (轮询)</option>` +
        this.accounts.map(a => `<option value="${a.id}">${this.escapeHtml(a.name)}</option>`).join("");
    }
  },

  async checkAccount(accId) {
    this.showToast("正在检测账号连通性...", "info");
    const res = await this.apiPost(`/api/accounts/${accId}/check`);
    if (res.ok) {
      this.showToast(`连通性测试通过！远端别名数: ${res.data.alias_count}`, "success");
      this.loadAccounts();
    } else {
      this.showToast(`测试失败：${res.error}`, "error");
      this.loadAccounts();
    }
  },

  async syncAccount(accId) {
    this.showToast("正在从 iCloud 远端同步所有别名...", "info");
    const res = await this.apiPost(`/api/accounts/${accId}/sync`);
    if (res.ok) {
      this.showToast(`同步成功！远端总数: ${res.data.total_remote}，本地新增: ${res.data.new_saved}`, "success");
      this.loadAccounts();
      this.loadStats();
    } else {
      this.showToast(`同步失败：${res.error}`, "error");
    }
  },

  async clearAccountCooldown(accId) {
    const res = await this.apiPost(`/api/accounts/${accId}/clear-cooldown`);
    if (res.ok) {
      this.showToast("已成功解除账号限流冷却！", "success");
      this.loadAccounts();
    }
  },

  async deleteAccount(accId, name) {
    if (!confirm(`确定要删除账号 [${name}] 吗？`)) return;
    const res = await this.apiDelete(`/api/accounts/${accId}`);
    if (res.ok) {
      this.showToast("账号已删除", "success");
      this.loadAccounts();
      this.loadStats();
    } else {
      this.showToast(res.error || "删除失败", "error");
    }
  },

  // ==========================
  // ACCOUNT PROXY & 750 AUTOPILOT
  // ==========================

  openProxyModal(accId) {
    const acc = this.accounts.find(a => a.id === accId);
    if (!acc) return;
    document.getElementById("proxy-account-id").value = acc.id;
    document.getElementById("proxy-input-url").value = acc.proxy_url || "";
    document.getElementById("proxy-input-target").value = acc.target_alias_count || 750;
    const resBox = document.getElementById("proxy-test-result");
    if (resBox) resBox.style.display = "none";
    this.openModal("modal-proxy");
  },

  closeProxyModal() {
    this.closeModal("modal-proxy");
  },

  async testProxyConnection() {
    const proxyUrl = document.getElementById("proxy-input-url").value.trim();
    const accId = document.getElementById("proxy-account-id").value;
    const resBox = document.getElementById("proxy-test-result");

    if (!proxyUrl) {
      resBox.style.display = "block";
      resBox.style.background = "#fef2f2";
      resBox.style.color = "#991b1b";
      resBox.textContent = "请输入代理地址再进行测试";
      return;
    }

    resBox.style.display = "block";
    resBox.style.background = "#eff6ff";
    resBox.style.color = "#1e40af";
    resBox.textContent = "正在测试代理网络连通性与公网出口 IP...";

    const res = await this.apiPost(`/api/accounts/${accId}/proxy/test`, { proxy_url: proxyUrl });
    if (res.ok && res.data) {
      resBox.style.background = "#ecfdf5";
      resBox.style.color = "#065f46";
      resBox.innerHTML = `✅ 代理连通正常！<br>• 出口公网 IP: <strong>${res.data.exit_ip}</strong><br>• 握手延迟: <strong>${res.data.latency_ms} ms</strong>`;
    } else {
      resBox.style.background = "#fef2f2";
      resBox.style.color = "#991b1b";
      resBox.textContent = `❌ ${res.error || "代理连接失败，请检查 IP/端口/鉴权"}`;
    }
  },

  async saveAccountProxy() {
    const accId = document.getElementById("proxy-account-id").value;
    const proxyUrl = document.getElementById("proxy-input-url").value.trim();
    const targetCount = parseInt(document.getElementById("proxy-input-target").value, 10) || 750;

    const res = await this.apiPost(`/api/accounts/${accId}/proxy`, {
      proxy_url: proxyUrl,
      target_alias_count: targetCount,
    });

    if (res.ok) {
      this.showToast("账号专属代理与 750 目标配置已保存！", "success");
      this.closeProxyModal();
      this.loadAccounts();
    } else {
      this.showToast(res.error || "保存失败", "error");
    }
  },

  async startAccountAutopilot(accId) {
    const acc = this.accounts.find(a => a.id === accId);
    if (!acc) return;
    const cur = acc.current_total_aliases || Math.max(acc.alias_count || 0, acc.local_alias_count || 0);
    const target = acc.target_alias_count || 750;
    const needed = Math.max(0, target - cur);

    if (needed <= 0) {
      this.showToast(`账号 [${acc.name}] 已达到 ${target} 满额，无需继续创建`, "info");
      return;
    }

    if (!confirm(`确定为账号 [${acc.name}] 启动专属无人值守托管吗？\n\n• 当前别名数：${cur} 个\n• 目标满额：${target} 个 (还需创建 ${needed} 个)\n• 绑定代理：${acc.proxy_url || "直连"}\n• 隔离保障：该账号将使用独立后台线程运行，遇到频控仅休眠自身，绝对不阻塞其它账号！`)) {
      return;
    }

    this.showToast(`正在启动账号 [${acc.name}] 专属 750 托管...`, "info");
    const res = await this.apiPost(`/api/autopilot/accounts/${accId}/start`, {
      target_count: target,
      base_label: "HME",
    });

    if (res.ok) {
      this.showToast(`🚀 账号 [${acc.name}] 专属托管已启动！`, "success");
      this.loadAccounts();
    } else {
      this.showToast(res.error || "启动失败", "error");
    }
  },

  async pauseAccountAutopilot(accId) {
    const res = await this.apiPost(`/api/autopilot/accounts/${accId}/pause`);
    if (res.ok) {
      this.showToast("已暂停该账号托管", "warning");
      this.loadAccounts();
    }
  },

  async resumeAccountAutopilot(accId) {
    const res = await this.apiPost(`/api/autopilot/accounts/${accId}/resume`);
    if (res.ok) {
      this.showToast("已恢复该账号托管", "success");
      this.loadAccounts();
    }
  },

  async stopAccountAutopilot(accId) {
    if (!confirm("确定要停止该账号的托管吗？")) return;
    const res = await this.apiPost(`/api/autopilot/accounts/${accId}/stop`);
    if (res.ok) {
      this.showToast("已停止该账号托管", "warning");
      this.loadAccounts();
    }
  },

  async startAllAccountsAutopilot() {
    if (this.accounts.length === 0) {
      this.showToast("当前暂无账号，请先导入账号", "error");
      return;
    }

    if (!confirm(`确定要启动全部 ${this.accounts.length} 个账号的并发满额 750 托管吗？\n\n• 系统将为每个账号创建独立的 Worker 线程\n• 各账号使用其专属代理，完全并发、互相隔离、互不阻塞\n• 遇频控单账号休眠，其余账号照常高速创建！`)) {
      return;
    }

    this.showToast("正在批量拉起各账号独立托管线程...", "info");
    const res = await this.apiPost("/api/autopilot/all/start", { target_count: 750 });
    if (res.ok) {
      this.showToast(`🚀 成功并发启动 ${res.data.started} 个账号的独立 750 托管！`, "success");
      this.loadAccounts();
    } else {
      this.showToast(res.error || "启动失败", "error");
    }
  },

  async stopAllAccountsAutopilot() {
    if (!confirm("确定要停止所有账号的托管任务吗？")) return;
    const res = await this.apiPost("/api/autopilot/all/stop");
    if (res.ok) {
      this.showToast("已停止全部账号的托管任务", "warning");
      this.loadAccounts();
    }
  },

  // ==========================
  // RESIN PROXY HUB
  // ==========================

  async loadProxyHub() {
    // 1. Load Resin status
    const statRes = await this.apiGet("/api/proxy/status");
    if (statRes.ok) {
      const d = statRes.data;
      const statusEl = document.getElementById("resin-status-val");
      if (statusEl) {
        if (d.running) {
          statusEl.innerHTML = `<span style="color:#10b981; font-weight:600;">🟢 运行中 (:${d.port})</span>`;
        } else {
          statusEl.innerHTML = `<span style="color:#ef4444; font-weight:600;">🔴 未启动</span>`;
        }
      }
      const nodesEl = document.getElementById("resin-nodes-val");
      if (nodesEl) nodesEl.textContent = d.total_nodes || 0;
      const ipsEl = document.getElementById("resin-ips-val");
      if (ipsEl) ipsEl.textContent = d.unique_healthy_egress_ips || d.unique_egress_ips || 0;
      const subsEl = document.getElementById("resin-subs-val");
      if (subsEl) subsEl.textContent = d.subscription_count || 0;
    }

    // 2. Load Subscriptions
    const subRes = await this.apiGet("/api/proxy/subscriptions");
    const tbody = document.getElementById("subscriptions-table-body");
    if (tbody) {
      if (!subRes.ok || !subRes.data || subRes.data.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="text-center text-muted" style="padding:24px;">暂未添加任何订阅源。点击右上角【➕ 添加订阅链接】录入你的 Clash / YAML 订阅。</td></tr>`;
      } else {
        tbody.innerHTML = subRes.data.map(s => {
          let displayUrl = s.url.length > 55 ? s.url.substring(0, 52) + "..." : s.url;
          return `
            <tr>
              <td><strong>${this.escapeHtml(s.name)}</strong></td>
              <td><code>${this.escapeHtml(displayUrl)}</code></td>
              <td><span class="badge badge-info">${s.node_count || 0} 节点</span></td>
              <td><span class="badge badge-success">${s.healthy_node_count || 0} 出口IP</span></td>
              <td><span class="text-success" style="font-size:12px;">已自动同步</span></td>
              <td>
                <button class="btn btn-sm btn-danger" onclick="app.deleteSubscription('${s.id}', '${this.escapeHtml(s.name)}')">🗑️ 删除</button>
              </td>
            </tr>
          `;
        }).join("");
      }
    }
  },

  openAddSubscriptionModal() {
    document.getElementById("sub-input-name").value = "";
    document.getElementById("sub-input-url").value = "";
    this.openModal("modal-add-subscription");
  },

  async saveSubscription() {
    const name = document.getElementById("sub-input-name").value.trim();
    const url = document.getElementById("sub-input-url").value.trim();
    if (!url) {
      this.showToast("请输入有效的订阅链接", "error");
      return;
    }

    this.showToast("正在向 Resin 网关提交并拉取订阅节点...", "info");
    const res = await this.apiPost("/api/proxy/subscriptions", { name, url });
    if (res.ok) {
      this.showToast("订阅源添加成功，Resin 正在后台解析并探活节点！", "success");
      this.closeModal("modal-add-subscription");
      this.loadProxyHub();
    } else {
      this.showToast(res.error || "添加失败", "error");
    }
  },

  async deleteSubscription(subId, name) {
    if (!confirm(`确定要删除订阅 [${name}] 吗？`)) return;
    const res = await this.apiDelete(`/api/proxy/subscriptions/${subId}`);
    if (res.ok) {
      this.showToast("订阅已删除", "success");
      this.loadProxyHub();
    } else {
      this.showToast(res.error || "删除失败", "error");
    }
  },

  async autoAssignResinProxies() {
    if (!confirm("确定要一键为全部账号自动配置 Resin 专属粘性代理吗？\n\n• 系统将自动为每个账号分配形如 http://Default.{账号ID}@127.0.0.1:2260 的专属代理；\n• Resin 会自动将每个账号绑定至固定出口 IP，保持会话粘性并实现 IP 物理隔离；\n• 遇节点失效或频控将自动智能熔断漂移至同 IP 节点。")) return;

    this.showToast("正在一键分配 Resin 粘性代理...", "info");
    const res = await this.apiPost("/api/proxy/accounts/auto-assign");
    if (res.ok) {
      this.showToast(`🎉 ${res.message}！`, "success");
      this.loadAccounts();
      this.switchTab("accounts");
    } else {
      this.showToast(res.error || "分配失败", "error");
    }
  },

  async restartResinGateway() {
    this.showToast("正在重启 Resin 智能代理网关...", "info");
    const res = await this.apiPost("/api/proxy/resin/restart");
    if (res.ok) {
      this.showToast("Resin 智能代理网关已重启并就绪！", "success");
      this.loadProxyHub();
    } else {
      this.showToast(res.error || "重启失败", "error");
    }
  },

  // ==========================
  // PROTOCOL IMPORT MODAL
  // ==========================

  protoImportTimer: null,

  openProtocolImportModal() {
    document.getElementById("proto-import-text").value = "";
    document.getElementById("proto-import-progress-box").style.display = "none";
    document.getElementById("proto-input-group").style.display = "block";
    const btn = document.getElementById("btn-start-proto-import");
    btn.disabled = false;
    btn.textContent = "🚀 开始全自动协议登录与导入";
    if (this.protoImportTimer) {
      clearInterval(this.protoImportTimer);
      this.protoImportTimer = null;
    }
    this.openModal("modal-protocol-import");
  },

  async startProtocolImport() {
    const text = document.getElementById("proto-import-text").value.trim();
    if (!text) {
      this.showToast("请先粘贴账号凭据行 (账号----密码----取码URL)", "error");
      return;
    }

    const channelInput = document.querySelector('input[name="proto-channel"]:checked');
    const channel = channelInput ? channelInput.value : "dual";

    const btn = document.getElementById("btn-start-proto-import");
    btn.disabled = true;
    btn.textContent = `⏳ 正在提交并启动协议握手 (${channel === "dual" ? "双通道" : channel})...`;

    const res = await this.apiPost("/api/accounts/protocol-import", { text, channel });
    if (!res.ok) {
      this.showToast(res.error || "提交失败", "error");
      btn.disabled = false;
      btn.textContent = "🚀 开始全自动协议登录与导入";
      return;
    }

    const job = res.data;
    document.getElementById("proto-import-progress-box").style.display = "block";
    this.renderProtoJobProgress(job);

    if (this.protoImportTimer) clearInterval(this.protoImportTimer);
    this.protoImportTimer = setInterval(async () => {
      const sRes = await this.apiGet(`/api/accounts/protocol-import/status?job_id=${job.id}`);
      if (sRes.ok && sRes.data) {
        const curJob = sRes.data;
        this.renderProtoJobProgress(curJob);
        if (curJob.status === "completed") {
          clearInterval(this.protoImportTimer);
          this.protoImportTimer = null;
          btn.disabled = false;
          btn.textContent = "✅ 全部账号处理完成";
          this.showToast(`协议导入完成！成功: ${curJob.success}，失败: ${curJob.failed}`, curJob.failed === 0 ? "success" : "info");
          this.loadAccounts();
          this.loadStats();
        }
      }
    }, 1500);
  },

  renderProtoJobProgress(job) {
    const total = job.total || 0;
    const finished = job.finished || 0;
    const pct = total > 0 ? Math.round((finished / total) * 100) : 0;

    document.getElementById("proto-prog-count").textContent = `${finished}/${total}`;
    document.getElementById("proto-bar-fill").style.width = `${pct}%`;

    const badge = document.getElementById("proto-job-badge");
    if (job.status === "completed") {
      badge.className = "badge badge-success";
      badge.textContent = "已完成";
    } else {
      badge.className = "badge badge-info";
      badge.textContent = `执行中 (${pct}%)`;
    }

    const container = document.getElementById("proto-items-table");
    container.innerHTML = job.items.map((it) => {
      let statusBadge = `<span class="badge badge-secondary">排队中</span>`;
      if (it.status === "running") {
        statusBadge = `<span class="badge badge-info">🔄 ${this.escapeHtml(it.step)}</span>`;
      } else if (it.status === "success") {
        statusBadge = `<span class="badge badge-success">✅ 已就绪</span>`;
      } else if (it.status === "failed") {
        statusBadge = `<span class="badge badge-danger">❌ 失败</span>`;
      }

      return `
        <div style="display:flex; justify-content:space-between; align-items:center; padding:8px 0; border-bottom:1px solid #f1f5f9;">
          <div>
            <div style="font-weight:600; color:#1e293b;">
              ${this.escapeHtml(it.apple_id)}
              <span class="badge ${it.channel === 'apple_account' ? 'badge-warning' : 'badge-primary'}" style="margin-left:6px; font-size:10px;">${it.channel || 'icloud_web'}</span>
            </div>
            <div style="color:#64748b; font-size:0.8rem; margin-top:2px;">
              ${this.escapeHtml(it.detail || "")}
              ${it.alias_count ? ` · 远端别名数: ${it.alias_count}` : ""}
            </div>
          </div>
          <div>${statusBadge}</div>
        </div>
      `;
    }).join("");
  },

  // ==========================
  // IMPORT MODAL
  // ==========================

  openImportModal() {
    document.getElementById("import-curl-text").value = "";
    document.getElementById("import-account-name").value = "";
    document.getElementById("import-preview-box").style.display = "none";
    this.openModal("modal-import");
  },

  async previewImportCurl() {
    const curl = document.getElementById("import-curl-text").value.trim();
    if (!curl) {
      this.showToast("请先粘贴 cURL 命令文本", "error");
      return;
    }
    const res = await this.apiPost("/api/accounts/parse", { text: curl });
    if (res.ok && res.data) {
      const box = document.getElementById("import-preview-box");
      const pre = document.getElementById("import-preview-json");
      pre.textContent = JSON.stringify(res.data, null, 2);
      box.style.display = "block";
      this.showToast("cURL 解析成功！请点击保存", "success");
    } else {
      this.showToast(`解析失败：${res.error}`, "error");
    }
  },

  async saveImportedAccount() {
    const curl = document.getElementById("import-curl-text").value.trim();
    const name = document.getElementById("import-account-name").value.trim();
    if (!curl) {
      this.showToast("请先粘贴 cURL 命令文本", "error");
      return;
    }

    this.showToast("正在解析并保存账号...", "info");
    const res = await this.apiPost("/api/accounts/import", { curl: curl, name: name });
    if (res.ok) {
      this.showToast(`账号 [${res.data.name}] 保存成功！`, "success");
      this.closeModal("modal-import");
      this.loadAccounts();
      this.loadStats();
    } else {
      this.showToast(`保存失败：${res.error}`, "error");
    }
  },

  openManualAccountModal() {
    this.openModal("modal-manual-account");
  },

  async saveManualAccount() {
    const name = document.getElementById("man-name").value.trim();
    const host = document.getElementById("man-host").value.trim();
    const dsid = document.getElementById("man-dsid").value.trim();
    const cookie = document.getElementById("man-cookie").value.trim();

    if (!host || !cookie) {
      this.showToast("Host 与 Cookie 为必填项", "error");
      return;
    }

    const res = await this.apiPost("/api/accounts/import", {
      name, host, dsid, cookie
    });
    if (res.ok) {
      this.showToast("手动账号添加成功！", "success");
      this.closeModal("modal-manual-account");
      this.loadAccounts();
      this.loadStats();
    } else {
      this.showToast(`添加失败：${res.error}`, "error");
    }
  },

  // ==========================
  // BATCH CREATION & SCHEDULER
  // ==========================

  onStrategyChange() {
    const strategy = document.getElementById("batch-strategy").value;
    const group = document.getElementById("group-target-account");
    group.style.display = strategy === "specific" ? "block" : "none";
  },

  setBatchCount(val) {
    const el = document.getElementById("batch-count");
    if (el) el.value = val;
  },

  async loadAutopilotInfo() {
    const res = await this.apiGet("/api/tasks/autopilot/info");
    if (res.ok && res.data) {
      this.autopilotInfo = res.data;
      const curEl = document.getElementById("ap-stat-current");
      const nedEl = document.getElementById("ap-stat-needed");
      if (curEl) curEl.textContent = res.data.alias_count;
      if (nedEl) nedEl.textContent = res.data.needed;
    }
  },

  async startAutopilotToTarget() {
    if (!this.autopilotInfo) await this.loadAutopilotInfo();
    const needed = this.autopilotInfo ? this.autopilotInfo.needed : 750;
    if (needed <= 0) {
      this.showToast("当前账号已达到 750 个别名满额，无需继续创建！", "info");
      return;
    }

    if (!confirm(`确定要启动全自动无人值守托管吗？\n\n• 当前账号已有：${this.autopilotInfo.alias_count} 个别名\n• 本次将自动创建：${needed} 个别名（补齐至 750 满额）\n• 模式：自动轮换 Apple/iCloud 双通道，遇官方频控自动休眠倒计时后唤醒继续，保活守护全程在线，无需人工干预。`)) {
      return;
    }

    this.showToast(`正在启动全自动托管任务 (补齐 ${needed} 个别名)...`, "info");
    const res = await this.apiPost("/api/tasks/start", {
      count: needed,
      strategy: "round_robin",
      base_label: "HME",
      note: "自动托管补齐至750",
      auto_pilot: true,
    });

    if (res.ok) {
      this.showToast("🚀 全自动托管任务已成功启动！系统将自动无人值守执行至满额", "success");
      this.checkCurrentJob();
    } else {
      this.showToast(`托管任务启动失败：${res.error}`, "error");
    }
  },

  async submitBatchJob(e) {
    e.preventDefault();
    const count = parseInt(document.getElementById("batch-count").value, 10);
    const strategy = document.getElementById("batch-strategy").value;
    const targetAccount = strategy === "specific" ? document.getElementById("batch-target-account").value : "";
    const baseLabel = document.getElementById("batch-label").value.trim() || "Auto";
    const note = document.getElementById("batch-note").value.trim();
    const autoPilot = document.getElementById("batch-auto-pilot") ? document.getElementById("batch-auto-pilot").checked : true;

    if (this.accounts.length === 0) {
      this.showToast("当前暂无 iCloud 账号，请先导入账号", "error");
      return;
    }

    this.showToast("正在提交批量创建任务...", "info");
    const res = await this.apiPost("/api/tasks/start", {
      count, strategy, target_account_id: targetAccount, base_label: baseLabel, note, auto_pilot: autoPilot
    });

    if (res.ok) {
      this.showToast("批量任务启动成功！", "success");
      this.checkCurrentJob();
    } else {
      this.showToast(`任务启动失败：${res.error}`, "error");
    }
  },

  async checkCurrentJob() {
    const res = await this.apiGet("/api/tasks/current");
    const job = res.ok ? res.data : null;
    this.activeJob = job;

    const noTaskPlaceholder = document.getElementById("no-task-placeholder");
    const taskDetails = document.getElementById("active-task-details");
    const taskCtrls = document.getElementById("task-ctrl-buttons");
    const dashActiveTask = document.getElementById("dashboard-active-task");
    const cooldownBox = document.getElementById("autopilot-cooldown-box");

    if (!job || job.status === "completed" || job.status === "cancelled" || job.status === "failed") {
      if (noTaskPlaceholder) noTaskPlaceholder.style.display = "block";
      if (taskDetails) taskDetails.style.display = "none";
      if (taskCtrls) taskCtrls.style.display = "none";
      if (dashActiveTask) dashActiveTask.style.display = "none";
      if (cooldownBox) cooldownBox.style.display = "none";
      return;
    }

    // Active job present
    if (noTaskPlaceholder) noTaskPlaceholder.style.display = "none";
    if (taskDetails) taskDetails.style.display = "block";
    if (taskCtrls) taskCtrls.style.display = "flex";
    if (dashActiveTask) dashActiveTask.style.display = "block";

    const percent = job.progress_percent || 0;
    const progressText = `进度: ${job.created_count} / ${job.total_requested} (${percent}%)`;
    const accountText = `执行账号: ${job.current_account_name || "-"}`;

    // Handle waiting_cooldown state (Autopilot countdown)
    if (job.status === "waiting_cooldown") {
      if (cooldownBox) {
        cooldownBox.style.display = "flex";
        const now = Math.floor(Date.now() / 1000);
        const rem = Math.max(0, (job.cooldown_resume_at || 0) - now);
        const mins = String(Math.floor(rem / 60)).padStart(2, "0");
        const secs = String(rem % 60).padStart(2, "0");
        const timerEl = document.getElementById("ap-countdown-timer");
        const msgEl = document.getElementById("ap-cooldown-msg");
        if (timerEl) timerEl.textContent = `${mins}:${secs}`;
        if (msgEl && job.last_error) msgEl.textContent = job.last_error;
      }
    } else {
      if (cooldownBox) cooldownBox.style.display = "none";
    }

    // Update batch view
    document.getElementById("batch-prog-counter").textContent = progressText;
    document.getElementById("batch-prog-account").textContent = accountText;
    document.getElementById("batch-prog-bar").style.width = `${percent}%`;

    // Update dashboard widget
    let statusText = job.status.toUpperCase();
    if (job.status === "waiting_cooldown") {
      statusText = "自动休眠等待中 (倒计时)";
    }
    document.getElementById("dash-task-status").textContent = statusText;
    document.getElementById("dash-task-progress-text").textContent = progressText;
    document.getElementById("dash-task-account-text").textContent = accountText;
    document.getElementById("dash-task-bar-fill").style.width = `${percent}%`;

    // Update pause buttons
    const pauseBtn = document.getElementById("btn-batch-pause");
    const dashPauseBtn = document.getElementById("dash-btn-pause");
    const isPaused = job.status === "paused";
    if (pauseBtn) pauseBtn.textContent = isPaused ? "继续执行" : "暂停";
    if (dashPauseBtn) dashPauseBtn.textContent = isPaused ? "继续执行" : "暂停";

    // Render items
    const itemsCont = document.getElementById("batch-items-container");
    if (itemsCont && job.items) {
      itemsCont.innerHTML = job.items.map(it => {
        let badge = `<span class="badge badge-primary">${it.status}</span>`;
        if (it.status === "success") badge = `<span class="badge badge-success">✓ 成功</span>`;
        else if (it.status === "failed") badge = `<span class="badge badge-danger">✗ 失败</span>`;
        else if (it.status === "generating" || it.status === "reserving") badge = `<span class="badge badge-warning">⚡ 进行中</span>`;
        else if (it.status === "pending") badge = `<span class="badge badge-secondary" style="background:#f1f5f9; color:#64748b;">等待中</span>`;

        return `
          <div class="batch-item-row">
            <div>
              <strong>#${it.position} [${this.escapeHtml(it.label)}]</strong>
              <span class="text-muted" style="margin-left:8px;">${it.created_email ? `<code>${it.created_email}</code>` : (it.candidate_email || "等待生成")}</span>
            </div>
            <div>
              ${badge}
            </div>
          </div>
        `;
      }).join("");
    }
  },

  async forceWakeupTask() {
    this.showToast("正在唤醒自动托管工作线程...", "info");
    const res = await this.apiPost("/api/tasks/resume");
    if (res.ok) {
      this.showToast("已成功唤醒！系统正在重新尝试创建", "success");
      this.checkCurrentJob();
    } else {
      this.showToast(`唤醒失败：${res.error}`, "error");
    }
  },

  async pauseTask() {
    if (!this.activeJob) return;
    if (this.activeJob.status === "paused") {
      const res = await this.apiPost("/api/tasks/resume");
      if (res.ok) this.showToast("任务已恢复", "info");
    } else {
      const res = await this.apiPost("/api/tasks/pause");
      if (res.ok) this.showToast("任务已暂停", "warning");
    }
    this.checkCurrentJob();
  },

  async cancelTask() {
    if (!confirm("确定要取消当前的批量创建任务吗？")) return;
    const res = await this.apiPost("/api/tasks/cancel");
    if (res.ok) {
      this.showToast("任务已取消", "warning");
      this.checkCurrentJob();
    }
  },

  startJobPolling() {
    setInterval(() => {
      if (this.currentTab === "dashboard" || this.currentTab === "batch") {
        this.checkCurrentJob();
      }
      if (this.currentTab === "accounts") {
        this.loadAccounts();
      }
    }, 2500);
  },

  // ==========================
  // ALIASES & EXPORT
  // ==========================

  async loadAliases() {
    const accFilter = document.getElementById("alias-account-filter")?.value || "";
    const search = document.getElementById("alias-search")?.value || "";
    const offset = this.aliasPage * this.aliasLimit;

    let url = `/api/aliases?limit=${this.aliasLimit}&offset=${offset}`;
    if (accFilter) url += `&account_id=${encodeURIComponent(accFilter)}`;
    if (search) url += `&search=${encodeURIComponent(search)}`;

    const res = await this.apiGet(url);
    if (!res.ok) {
      this.showToast(res.error || "获取别名列表失败", "error");
      return;
    }

    this.aliases = res.data.list || [];
    this.aliasTotal = res.data.total || 0;
    this.renderAliasesTable();
  },

  renderAliasesTable() {
    const tbody = document.getElementById("aliases-table-body");
    if (this.aliases.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="text-center text-muted" style="padding: 30px;">暂无别名邮箱记录</td></tr>`;
      document.getElementById("alias-pagination-text").textContent = "共 0 条记录";
      return;
    }

    tbody.innerHTML = this.aliases.map((al) => {
      const timeStr = al.created_at ? new Date(al.created_at * 1000).toLocaleString() : "-";
      return `
        <tr>
          <td>
            <div style="display:flex; align-items:center;">
              <strong><code>${this.escapeHtml(al.email)}</code></strong>
              <button class="copy-btn" onclick="app.copyText('${this.escapeHtml(al.email)}')" title="点击复制邮箱">📋</button>
            </div>
          </td>
          <td><span class="badge badge-primary">${this.escapeHtml(al.account_name || "未知")}</span></td>
          <td>${this.escapeHtml(al.label || "-")}</td>
          <td><span class="text-muted">${this.escapeHtml(al.note || "-")}</span></td>
          <td><span class="text-muted">${this.escapeHtml(al.forward_to || "-")}</span></td>
          <td style="font-size:12px; color:#64748b;">${timeStr}</td>
          <td>
            <button class="btn btn-sm btn-danger" onclick="app.deleteAlias('${al.id}', '${this.escapeHtml(al.email)}')" title="从本地及远端删除">🗑️ 删除</button>
          </td>
        </tr>
      `;
    }).join("");

    const start = this.aliasPage * this.aliasLimit + 1;
    const end = Math.min(this.aliasTotal, (this.aliasPage + 1) * this.aliasLimit);
    document.getElementById("alias-pagination-text").textContent = `显示 ${start}-${end}，共 ${this.aliasTotal} 条记录`;
    document.getElementById("alias-page-num").textContent = `第 ${this.aliasPage + 1} 页`;
    document.getElementById("alias-prev-page").disabled = this.aliasPage === 0;
    document.getElementById("alias-next-page").disabled = end >= this.aliasTotal;
  },

  prevAliasPage() {
    if (this.aliasPage > 0) {
      this.aliasPage--;
      this.loadAliases();
    }
  },

  nextAliasPage() {
    if ((this.aliasPage + 1) * this.aliasLimit < this.aliasTotal) {
      this.aliasPage++;
      this.loadAliases();
    }
  },

  searchAliases() {
    this.aliasPage = 0;
    this.loadAliases();
  },

  filterAliasesByAccount() {
    this.aliasPage = 0;
    this.loadAliases();
  },

  createSingleAliasModal() {
    this.openModal("modal-single-create");
  },

  async executeSingleCreate() {
    const accId = document.getElementById("single-create-account").value;
    const label = document.getElementById("single-create-label").value.trim();
    const note = document.getElementById("single-create-note").value.trim();

    this.showToast("正在创建单个别名...", "info");
    const res = await this.apiPost("/api/aliases/create", {
      account_id: accId || null,
      label,
      note,
    });

    if (res.ok) {
      this.showToast(`创建成功：${res.data.email}`, "success");
      this.closeModal("modal-single-create");
      this.loadAliases();
      this.loadStats();
      this.loadAccounts();
    } else {
      this.showToast(`创建失败：${res.error}`, "error");
    }
  },

  async deleteAlias(aliasId, email) {
    const remote = confirm(`删除别名 [${email}]\n\n点击“确定”将在本地及 iCloud 远端彻底删除；\n点击“取消”放弃操作。`);
    if (!remote) return;

    const res = await this.apiDelete(`/api/aliases/${aliasId}?remote=1`);
    if (res.ok) {
      this.showToast("别名已删除", "success");
      this.loadAliases();
      this.loadStats();
    } else {
      this.showToast(res.error || "删除失败", "error");
    }
  },

  exportAliases(format) {
    const accFilter = document.getElementById("alias-account-filter")?.value || "";
    let url = `/api/export?format=${format}`;
    if (accFilter) url += `&account_id=${encodeURIComponent(accFilter)}`;
    window.open(url, "_blank");
  },

  // ==========================
  // REALTIME LOGS
  // ==========================

  setLogFilter(lvl) {
    this.logFilter = lvl;
    document.querySelectorAll(".log-filter-btn").forEach(b => {
      b.classList.toggle("active", b.getAttribute("data-filter") === lvl);
    });
    this.renderLogs();
  },

  async loadLogs() {
    try {
      const res = await this.apiGet("/api/logs?limit=150");
      if (res.ok && Array.isArray(res.data)) {
        this.logsCache = res.data;
        this.renderLogs();
      } else {
        const term = document.getElementById("log-terminal");
        if (term) term.innerHTML = `<div class="log-line text-danger">日志获取异常：${this.escapeHtml(res.error || '未返回有效数据')}</div>`;
      }
    } catch (e) {
      console.error("loadLogs error:", e);
      const term = document.getElementById("log-terminal");
      if (term) term.innerHTML = `<div class="log-line text-danger">日志加载失败：${this.escapeHtml(e.message)}</div>`;
    }
  },

  renderLogs() {
    const items = this.logsCache || [];
    let filtered = items;
    if (this.logFilter) {
      filtered = filtered.filter(l => (l.level || "").toUpperCase() === this.logFilter);
    }

    const countEl = document.getElementById("log-count-indicator");
    if (countEl) countEl.textContent = `共 ${filtered.length} 条`;

    // 1. Render in main terminal (#log-terminal)
    const term = document.getElementById("log-terminal");
    if (term) {
      if (filtered.length === 0) {
        term.innerHTML = `<div class="log-line text-muted">暂无日志记录</div>`;
      } else {
        term.innerHTML = filtered.map(l => {
          const time = l.created_at ? new Date(l.created_at * 1000).toLocaleTimeString() : "--:--:--";
          const accBadge = l.account_name ? `<span class="badge" style="background:#334155; color:#94a3b8; font-size:10px; margin-right:4px;">${this.escapeHtml(l.account_name)}</span>` : "";
          return `<div class="log-line"><span style="color:#64748b;">[${time}]</span> <span class="log-level-${l.level}">[${l.level}]</span> ${accBadge}${this.escapeHtml(l.message)}</div>`;
        }).join("");
      }
    }

    // 2. Render in dashboard recent terminal (#dash-recent-logs)
    const dashTerm = document.getElementById("dash-recent-logs");
    if (dashTerm) {
      const recent = items.slice(0, 8);
      if (recent.length === 0) {
        dashTerm.innerHTML = `<div class="log-line text-muted">暂无最近事件</div>`;
      } else {
        dashTerm.innerHTML = recent.map(l => {
          const time = l.created_at ? new Date(l.created_at * 1000).toLocaleTimeString() : "--:--:--";
          const accBadge = l.account_name ? `<span class="badge" style="background:#334155; color:#94a3b8; font-size:9px; margin-right:4px;">${this.escapeHtml(l.account_name)}</span>` : "";
          return `<div class="log-line"><span style="color:#64748b;">[${time}]</span> <span class="log-level-${l.level}">[${l.level}]</span> ${accBadge}${this.escapeHtml(l.message)}</div>`;
        }).join("");
      }
    }
  },

  startLogPolling() {
    setInterval(() => {
      const auto = document.getElementById("log-auto-refresh")?.checked ?? true;
      if (auto) {
        if (this.currentTab === "logs" || this.currentTab === "dashboard") {
          this.loadLogs();
        }
      }
    }, 3000);
  },

  // ==========================
  // UI UTILITIES
  // ==========================

  openModal(modalId) {
    const el = document.getElementById(modalId);
    if (el) el.classList.add("active");
  },

  closeModal(modalId) {
    const el = document.getElementById(modalId);
    if (el) el.classList.remove("active");
  },

  showToast(message, type = "info") {
    const cont = document.getElementById("toast-container");
    const toast = document.createElement("div");
    toast.className = `toast toast-${type}`;
    const icons = { success: "✅", error: "❌", info: "ℹ️", warning: "⚠️" };
    toast.innerHTML = `<span>${icons[type] || ""}</span> <span>${this.escapeHtml(message)}</span>`;
    cont.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = "0";
      toast.style.transform = "translateY(10px)";
      setTimeout(() => toast.remove(), 300);
    }, 3500);
  },

  copyText(text) {
    navigator.clipboard.writeText(text).then(() => {
      this.showToast(`已复制到剪贴板：${text}`, "success");
    }).catch(() => {
      this.showToast("复制失败，请手动复制", "error");
    });
  },

  escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  },
};

// Ensure app is globally accessible on window for inline event handlers and DevTools
window.app = app;

// Initialize app when DOM is ready
document.addEventListener("DOMContentLoaded", () => {
  app.init();
});
