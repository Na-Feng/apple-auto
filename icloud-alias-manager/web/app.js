// iCloud Alias Manager - Frontend Application Logic

const app = {
  currentTab: "dashboard",
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
    } else if (this.currentTab === "batch") {
      this.loadAccounts();
      this.checkCurrentJob();
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
      tbody.innerHTML = `<tr><td colspan="8" class="text-center text-muted" style="padding: 30px;">暂无 iCloud 账号，请点击右上角导入</td></tr>`;
      return;
    }

    tbody.innerHTML = this.accounts.map((acc) => {
      let statusBadge = `<span class="badge badge-success">🟢 正常</span>`;
      let cooldownText = `<span class="text-muted">-</span>`;

      if (acc.is_cooling_down) {
        statusBadge = `<span class="badge badge-warning">⏳ 冷却中</span>`;
        cooldownText = `<span class="badge badge-warning">剩余 ${acc.cooldown_remaining_seconds}s (阶: ${acc.cooldown_stage})</span>`;
      } else if (acc.status === "need_login") {
        statusBadge = `<span class="badge badge-danger">🔴 凭证失效</span>`;
        cooldownText = `<span class="text-danger">${acc.last_error || "需重新导入"}</span>`;
      }

      return `
        <tr>
          <td><strong>${this.escapeHtml(acc.name)}</strong></td>
          <td>
            <div style="font-size:11px;">
              <span class="badge badge-primary">${acc.channel}</span><br>
              <code>${this.escapeHtml(acc.host)}</code>
            </div>
          </td>
          <td>${statusBadge}</td>
          <td><strong>${acc.alias_count}</strong></td>
          <td><span style="color:#2563eb; font-weight:600;">${acc.local_alias_count}</span></td>
          <td><span class="text-muted">${this.escapeHtml(acc.selected_forward_to || "未设置")}</span></td>
          <td>${cooldownText}</td>
          <td>
            <div style="display:flex; gap:6px;">
              <button class="btn btn-sm btn-secondary" onclick="app.checkAccount('${acc.id}')" title="连通性测试">⚡ 测试</button>
              <button class="btn btn-sm btn-secondary" onclick="app.syncAccount('${acc.id}')" title="拉取远端已有别名">🔄 同步</button>
              ${acc.is_cooling_down ? `<button class="btn btn-sm btn-warning" onclick="app.clearAccountCooldown('${acc.id}')" title="手动解除冷却">❄️ 解除</button>` : ""}
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

    const btn = document.getElementById("btn-start-proto-import");
    btn.disabled = true;
    btn.textContent = "⏳ 正在提交并启动协议握手...";

    const res = await this.apiPost("/api/accounts/protocol-import", { text });
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
            <div style="font-weight:600; color:#1e293b;">${this.escapeHtml(it.apple_id)}</div>
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

  async submitBatchJob(e) {
    e.preventDefault();
    const count = parseInt(document.getElementById("batch-count").value, 10);
    const strategy = document.getElementById("batch-strategy").value;
    const targetAccount = strategy === "specific" ? document.getElementById("batch-target-account").value : "";
    const baseLabel = document.getElementById("batch-label").value.trim() || "Auto";
    const note = document.getElementById("batch-note").value.trim();

    if (this.accounts.length === 0) {
      this.showToast("当前暂无 iCloud 账号，请先导入账号", "error");
      return;
    }

    this.showToast("正在提交批量创建任务...", "info");
    const res = await this.apiPost("/api/tasks/start", {
      count, strategy, target_account_id: targetAccount, base_label: baseLabel, note
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

    if (!job || job.status === "completed" || job.status === "cancelled" || job.status === "failed") {
      if (noTaskPlaceholder) noTaskPlaceholder.style.display = "block";
      if (taskDetails) taskDetails.style.display = "none";
      if (taskCtrls) taskCtrls.style.display = "none";
      if (dashActiveTask) dashActiveTask.style.display = "none";
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

    // Update batch view
    document.getElementById("batch-prog-counter").textContent = progressText;
    document.getElementById("batch-prog-account").textContent = accountText;
    document.getElementById("batch-prog-bar").style.width = `${percent}%`;

    // Update dashboard widget
    document.getElementById("dash-task-status").textContent = job.status.toUpperCase();
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
    }, 2000);
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

  async loadLogs() {
    const res = await this.apiGet("/api/logs?limit=80");
    if (res.ok && res.data) {
      const term = document.getElementById("log-terminal");
      if (res.data.length === 0) {
        term.innerHTML = `<div class="log-line text-muted">暂无日志记录</div>`;
        return;
      }
      term.innerHTML = res.data.map(l => {
        const time = new Date(l.created_at * 1000).toLocaleTimeString();
        return `<div class="log-line"><span style="color:#64748b;">[${time}]</span> <span class="log-level-${l.level}">[${l.level}]</span> ${this.escapeHtml(l.message)}</div>`;
      }).join("");
    }
  },

  startLogPolling() {
    setInterval(() => {
      const auto = document.getElementById("log-auto-refresh")?.checked;
      if (this.currentTab === "logs" && auto) {
        this.loadLogs();
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

// Initialize app when DOM is ready
document.addEventListener("DOMContentLoaded", () => {
  app.init();
});
