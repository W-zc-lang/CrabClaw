/* 本地 AI 对话 —— 前端主逻辑 */

(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const api = () => window.pywebview.api;

  const state = {
    currentId: null,
    ollamaRunning: false,
    generating: false,
    models: [],
    settings: {},
    buffers: {}, // msgId -> 已累积的原始文本
    streaming: {}, // msgId -> 是否正在流式
    kbSources: {}, // msgId -> 参考文档名列表
    sheetGenerating: false,
    sheetBuffers: {}, // sheet_id -> 累积文本
    currentSheetId: null,
    autoPath: null,
    renamePlan: null,
    pendingFileOp: null, // 待确认的文件操作 {op, args, preview}
    fileControlEnabled: false,
    fileControlDisclaimerAccepted: false,
    bionicEnabled: false,
  };

  // ---------- 工具 ----------
  function b64decode(b64) {
    const bin = atob(b64);
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
    return new TextDecoder().decode(bytes);
  }

  function currentModel() {
    const sel = $("#model-select");
    return sel && sel.value ? sel.value : (state.settings.model || "");
  }

  function autoResize() {
    const ta = $("#input");
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 180) + "px";
  }

  function scrollBottom() {
    const m = $("#messages");
    m.scrollTop = m.scrollHeight;
  }

  function showBanner(msg, withStartBtn) {
    const b = $("#banner");
    b.innerHTML = "";
    const span = document.createElement("span");
    span.textContent = msg;
    b.appendChild(span);
    if (withStartBtn) {
      const btn = document.createElement("button");
      btn.textContent = "启动 Ollama";
      btn.onclick = startOllama;
      b.appendChild(btn);
    }
    b.classList.remove("hidden");
  }
  function hideBanner() {
    $("#banner").classList.add("hidden");
  }

  // ---------- 会话列表 ----------
  function renderSessions(sessions) {
    const box = $("#session-list");
    box.innerHTML = "";
    sessions.forEach((s) => {
      const el = document.createElement("div");
      el.className = "session-item" + (s.id === state.currentId ? " active" : "");
      el.dataset.id = s.id;

      const t = document.createElement("span");
      t.className = "session-title-text";
      t.textContent = s.title || "新对话";
      el.appendChild(t);

      const del = document.createElement("span");
      del.className = "session-del";
      del.textContent = "删除";
      del.onclick = (e) => {
        e.stopPropagation();
        deleteSession(s.id);
      };
      el.appendChild(del);

      el.onclick = () => openSession(s.id);
      box.appendChild(el);
    });
  }

  // ---------- 模型选择 ----------
  function renderModels(models) {
    state.models = models || [];
    const sel = $("#model-select");
    const set = $("#set-model");
    sel.innerHTML = "";
    set.innerHTML = "";
    if (!state.models.length) {
      const o = document.createElement("option");
      o.textContent = "（无模型，去下载）";
      o.value = "";
      sel.appendChild(o);
    } else {
      state.models.forEach((m) => {
        const o1 = document.createElement("option");
        o1.value = m.name;
        o1.textContent = m.name;
        sel.appendChild(o1);
        const o2 = o1.cloneNode(true);
        set.appendChild(o2);
      });
    }
    const cur = currentModel();
    if (cur) {
      sel.value = cur;
      set.value = cur;
    }
    const mini = $("#model-mini");
    mini.textContent = "模型：" + (cur || "未选择");
  }

  // ---------- 设置面板 ----------
  function fillSettings(s) {
    state.settings = s || {};
    $("#set-system").value = state.settings.system_prompt || "";
    $("#set-think").value = String(state.settings.thinking_level ?? 2);
    $("#set-temp").value = state.settings.temperature ?? 0.7;
    $("#val-temp").textContent = state.settings.temperature ?? 0.7;
    $("#set-topp").value = state.settings.top_p ?? 0.9;
    $("#val-topp").textContent = state.settings.top_p ?? 0.9;
    $("#set-ctx").value = state.settings.num_ctx ?? 8192;
    $("#set-keep").value = state.settings.keep_alive ?? "5m";
    $("#set-bionic").checked = !!state.settings.bionic_enabled;
    state.bionicEnabled = !!state.settings.bionic_enabled;
    $("#set-file-control").checked = !!state.settings.file_control_enabled;
    state.fileControlEnabled = !!state.settings.file_control_enabled;
    state.fileControlDisclaimerAccepted = !!state.settings.file_control_disclaimer_accepted;
    updateFileControlStatus();
    if (state.settings.model) {
      $("#model-select").value = state.settings.model;
      $("#set-model").value = state.settings.model;
    }
    applyBionicClass();
  }

  // ---------- 消息渲染 ----------
  function addBubble(role, text, msgId, streaming) {
    const wrap = document.createElement("div");
    wrap.className = "msg " + role;
    if (msgId != null) wrap.dataset.msgId = msgId;

    const av = document.createElement("div");
    av.className = "avatar";
    av.textContent = role === "user" ? "你" : "AI";
    wrap.appendChild(av);

    const bubble = document.createElement("div");
    bubble.className = "bubble" + (streaming ? " streaming" : "");
    if (text) bubble.innerHTML = MD.render(text);
    wrap.appendChild(bubble);
    $("#messages").appendChild(wrap);
    return wrap;
  }

  function getBubble(msgId) {
    return document.querySelector('.msg[data-msg-id="' + msgId + '"]');
  }

  function applyBionicToElement(el) {
    if (!state.bionicEnabled) return;
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null, false);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach((node) => {
      const text = node.textContent;
      if (!text.trim()) return;
      const span = document.createElement("span");
      span.innerHTML = text.replace(/([^\s<>]+)/g, (word) => {
        if (word.length <= 1) return word;
        const mid = Math.max(1, Math.ceil(word.length * 0.45));
        return "<strong>" + word.slice(0, mid) + "</strong>" + word.slice(mid);
      });
      node.parentNode.replaceChild(span, node);
    });
  }

  function paintBubble(msgId, streaming) {
    let el = getBubble(msgId);
    if (!el) el = addBubble("assistant", "", msgId, true);
    const bubble = el.querySelector(".bubble");
    const raw = state.buffers[msgId] || "";
    bubble.innerHTML = MD.render(raw) + (streaming ? '<span class="cursor"></span>' : "");
    if (state.bionicEnabled) applyBionicToElement(bubble);
    if (streaming) bubble.classList.add("streaming");
    scrollBottom();
  }

  function finalizeBubble(msgId, stats) {
    const el = getBubble(msgId);
    if (!el) return;
    const bubble = el.querySelector(".bubble");
    bubble.classList.remove("streaming");
    const raw = state.buffers[msgId] || "";
    bubble.innerHTML = MD.render(raw);
    if (state.bionicEnabled) applyBionicToElement(bubble);
    if (stats && (stats.tps || stats.tokens)) {
      let s = [];
      if (stats.tokens) s.push(stats.tokens + " tokens");
      if (stats.tps) s.push(stats.tps + " 字/秒");
      if (stats.prompt_tokens) s.push("上下文 " + stats.prompt_tokens);
      if (stats.load_s) s.push("载入 " + stats.load_s + "s");
      const foot = document.createElement("div");
      foot.className = "stats";
      foot.textContent = s.join(" · ");
      el.appendChild(foot);
    }
    // 操作按钮
    const actions = document.createElement("div");
    actions.className = "actions";
    const regen = document.createElement("button");
    regen.textContent = "重新生成";
    regen.onclick = () => regenerate(msgId);
    actions.appendChild(regen);
    el.appendChild(actions);

    // 知识库来源
    const src = state.kbSources[msgId];
    if (src && src.length) {
      const sd = document.createElement("div");
      sd.className = "kb-sources";
      const label = document.createElement("span");
      label.textContent = "📚 参考：";
      sd.appendChild(label);
      src.forEach((name) => {
        const chip = document.createElement("span");
        chip.className = "kb-chip";
        chip.textContent = name;
        sd.appendChild(chip);
      });
      el.appendChild(sd);
    }
  }

  function renderMessages(messages) {
    const box = $("#messages");
    box.innerHTML = "";
    messages.forEach((m) => {
      const el = addBubble(m.role, m.content, m.id, false);
      if (m.role === "assistant" && state.bionicEnabled) {
        const bubble = el.querySelector(".bubble");
        if (bubble) applyBionicToElement(bubble);
      }
      if (m.role === "assistant" && m.stats && (m.stats.tps || m.stats.tokens)) {
        const foot = document.createElement("div");
        foot.className = "stats";
        const s = [];
        if (m.stats.tokens) s.push(m.stats.tokens + " tokens");
        if (m.stats.tps) s.push(m.stats.tps + " 字/秒");
        foot.textContent = s.join(" · ");
        el.appendChild(foot);
        const actions = document.createElement("div");
        actions.className = "actions";
        const regen = document.createElement("button");
        regen.textContent = "重新生成";
        regen.onclick = () => regenerate(m.id);
        actions.appendChild(regen);
        el.appendChild(actions);
      }
    });
    scrollBottom();
  }

  // ---------- 生成状态 ----------
  function setGenerating(on) {
    state.generating = on;
    $("#btn-send").disabled = on;
    $("#btn-stop").classList.toggle("hidden", !on);
    const top = $("#topbar");
    top.querySelector(".model-select").disabled = on;
  }

  // ---------- 事件分发 ----------
  window.__pyEvent = function (b64) {
    let ev;
    try {
      ev = JSON.parse(b64decode(b64));
    } catch (e) {
      return;
    }
    switch (ev.type) {
      case "gen_start":
        state.generating = true;
        setGenerating(true);
        delete state.buffers[ev.msg_id];
        break;
      case "token":
        state.buffers[ev.msg_id] = (state.buffers[ev.msg_id] || "") + ev.text;
        paintBubble(ev.msg_id, true);
        break;
      case "gen_done":
        state.buffers[ev.msg_id] = state.buffers[ev.msg_id] || "";
        if (ev.error && !state.buffers[ev.msg_id].trim()) {
          // 模型不存在 / 启动失败：连一个字都没生成，删掉空气泡
          const emptyEl = getBubble(ev.msg_id);
          if (emptyEl) emptyEl.remove();
        } else {
          finalizeBubble(ev.msg_id, ev.stats);
        }
        if (ev.error) showBanner("生成出错：" + ev.error);
        setGenerating(false);
        break;
      case "kb_sources":
        state.kbSources[ev.msg_id] = ev.sources || [];
        break;
      case "gen_empty":
        {
          const el = getBubble(ev.msg_id);
          if (el) el.remove();
        }
        setGenerating(false);
        break;
      case "status":
        if (ev.state === "idle") setGenerating(false);
        break;
      case "session_renamed":
        renderSessions(ev.sessions);
        break;
      case "ollama_status":
        onOllamaStatus(ev);
        break;
      case "models":
        renderModels(ev.models);
        break;
      case "settings":
        fillSettings(ev.settings);
        break;
      case "pull":
        updatePull(ev);
        break;
      case "pull_done":
        closePull();
        break;
      case "pull_error":
        closePull();
        showBanner("下载失败：" + ev.message);
        break;
      case "sheet_start":
        state.sheetGenerating = true;
        state.sheetBuffers[ev.sheet_id] = "";
        $("#sheet-result").classList.remove("hidden");
        $("#sheet-result").innerHTML = '<div class="bubble streaming"></div>';
        break;
      case "sheet_token":
        state.sheetBuffers[ev.sheet_id] = (state.sheetBuffers[ev.sheet_id] || "") + ev.text;
        $("#sheet-result").innerHTML =
          '<div class="bubble">' +
          MD.render(state.sheetBuffers[ev.sheet_id]) +
          '<span class="cursor"></span></div>';
        break;
      case "sheet_done":
        state.sheetGenerating = false;
        if (ev.error) {
          showBanner("表格分析出错：" + ev.error);
        } else {
          $("#sheet-result").innerHTML =
            '<div class="bubble">' + MD.render(state.sheetBuffers[ev.sheet_id] || "") + "</div>";
        }
        break;
      // ---- 内置模型自动准备 ----
      case "model_status":
        showBanner(ev.message, false);
        break;
      case "model_error":
        showBanner("模型准备失败：" + ev.message);
        break;
      case "model_ready":
        showBanner("已就绪：" + ev.model + "，可以直接开聊了。", false);
        break;
      case "file_op_request":
        onFileOpRequest(ev);
        break;
      case "doc_summary":
        if (_summaryBtn) { _summaryBtn.disabled = false; _summaryBtn = null; }
        if (ev.error) {
          hideBanner();
          showBanner("摘要失败：" + (ev.error || ""));
        } else {
          hideBanner();
          $("#summary-body").innerHTML = MD.render(ev.summary || "（空）");
          $("#summary-modal").classList.remove("hidden");
        }
        break;
      case "file_op_done":
        hideBanner();
        if (!ev.result || !ev.result.ok) {
          const err = (ev.result && ev.result.error) || "未知错误";
          showBanner("文件操作失败：" + err);
          addSystemMessage("文件操作失败：" + err, true);
        } else {
          const msg = ev.result.message || "操作已完成";
          addSystemMessage("文件操作成功：" + msg, false);
          renderFileControlLog();
        }
        break;
    }
  };

  // ---------- 会话操作 ----------
  async function openSession(id) {
    state.currentId = id;
    const r = await api().open_session(id);
    if (!r.ok) return;
    $("#session-title").textContent = r.session.title || "新对话";
    if (r.session.model) {
      $("#model-select").value = r.session.model;
      $("#set-model").value = r.session.model;
      $("#model-mini").textContent = "模型：" + r.session.model;
    }
    renderMessages(r.messages);
    const sr = await api().list_sessions();
    renderSessions(sr.sessions);
  }

  async function deleteSession(id) {
    if (!confirm("删除这个会话？此操作不可撤销。")) return;
    const r = await api().delete_session(id);
    if (r.ok) {
      renderSessions(r.sessions);
      if (state.currentId === id) {
        state.currentId = null;
        $("#messages").innerHTML = "";
        $("#session-title").textContent = "新对话";
      }
    }
  }

  async function ensureSession() {
    if (state.currentId) return state.currentId;
    const r = await api().new_session($("#session-title").textContent || "新对话");
    state.currentId = r.session.id;
    renderSessions(r.sessions);
    return state.currentId;
  }

  // ---------- 发送 ----------
  async function send() {
    const text = $("#input").value.trim();
    if (!text || state.generating) return;
    const model = currentModel();
    if (!model) {
      showBanner("请先在右上角选择模型，或点左下角「下载模型」拉一个下来。");
      return;
    }
    const id = await ensureSession();
    $("#input").value = "";
    autoResize();
    addBubble("user", text, null, false);
    setGenerating(true);

    const res = await api().send(id, text);
    if (!res.ok) {
      showBanner(res.error || "发送失败");
      // 移除刚加的 user 气泡
      const last = $("#messages").lastElementChild;
      if (last) last.remove();
      setGenerating(false);
      return;
    }
    // 拿到后端真实 id 后再建占位气泡，避免流式 token 在 await 期间先到、
    // 由 paintBubble 自行创建出第二个同 id 气泡
    addBubble("assistant", "", res.assistant_msg_id, true);
    state.buffers[res.assistant_msg_id] = state.buffers[res.assistant_msg_id] || "";
    paintBubble(res.assistant_msg_id, true); // 冲刷可能已到达的 token
  }

  function stop() {
    api().stop();
  }

  async function regenerate(assistantId) {
    if (state.generating) return;
    const el = getBubble(assistantId);
    if (!el) return;
    // 删掉这条及之后的所有消息
    let n = el;
    while (n) {
      const next = n.nextElementSibling;
      n.remove();
      n = next;
    }
    setGenerating(true);
    await api().regenerate(state.currentId, assistantId);
  }

  // ---------- Ollama ----------
  async function startOllama() {
    showBanner("正在启动 Ollama，请稍候…（首次可能需要十几秒）");
    const r = await api().start_ollama();
    if (!r.ok) {
      showBanner(r.error || "启动失败");
    }
    // 启动结果（版本 / 模型列表）由后台线程通过 ollama_status 事件推送，
    // 这里不依赖返回值，主线程立即返回，窗口不会卡死。
  }

  function setOllamaStatus(ok, version) {
    const el = $("#ollama-status");
    el.className = "ollama-status " + (ok ? "ok" : "bad");
    el.textContent = ok
      ? "Ollama：已连接 (" + (version || "") + ")"
      : "Ollama：未连接";
  }

  function onOllamaStatus(ev) {
    state.ollamaRunning = ev.running;
    setOllamaStatus(ev.running, ev.version);
    if (ev.running) hideBanner();
    if (ev.models) {
      renderModels(ev.models);
    }
    // Ollama 已连接但缺少内置模型时，后台自动准备
    const hasBuiltin = (ev.models || []).some((m) => m.name === "local-assistant");
    if (ev.running && !hasBuiltin && !state._ensureStarted) {
      state._ensureStarted = true;
      showBanner(
        "正在准备本地 AI（首次需联网下载基础模型，约 2GB，请耐心等待）…",
        false
      );
      api().ensure_model();
    }
  }

  // ---------- 文件控制 ----------
  function onFileOpRequest(ev) {
    state.pendingFileOp = { op: ev.op, args: ev.args, session_id: ev.session_id };
    $("#file-op-preview").textContent = ev.preview;
    $("#file-op-modal").classList.remove("hidden");
  }

  async function confirmFileOp() {
    if (!state.pendingFileOp) return;
    const { op, args, session_id } = state.pendingFileOp;
    state._fileOpPending = { op, args, session_id };
    $("#file-op-modal").classList.add("hidden");
    state.pendingFileOp = null;
    showBanner("正在执行文件操作…", false);
    const r = await api().apply_file_op(session_id, op, args);
    if (!r.ok) {
      hideBanner();
      showBanner("文件操作失败：" + (r.error || "未知错误"));
    }
    // 结果通过 file_op_done 事件异步返回，这里不阻塞等待
  }

  function cancelFileOp() {
    state.pendingFileOp = null;
    $("#file-op-modal").classList.add("hidden");
    showBanner("已取消文件操作");
  }

  function addSystemMessage(text, isError) {
    const wrap = document.createElement("div");
    wrap.className = "message system";
    const av = document.createElement("div");
    av.className = "avatar";
    av.textContent = "⚙";
    av.style.background = isError ? "var(--red)" : "var(--muted)";
    const bubble = document.createElement("div");
    bubble.className = "bubble assistant";
    bubble.style.borderStyle = "dashed";
    bubble.textContent = text;
    wrap.appendChild(av);
    wrap.appendChild(bubble);
    $("#messages").appendChild(wrap);
    scrollBottom();
  }

  function openFileControlPanel() {
    $("#file-control-panel").classList.remove("hidden");
    if (state.fileControlEnabled) {
      $("#file-control-disabled").classList.add("hidden");
      $("#file-control-enabled").classList.remove("hidden");
      renderFileControlLog();
    } else {
      $("#file-control-disabled").classList.remove("hidden");
      $("#file-control-enabled").classList.add("hidden");
    }
  }

  async function renderFileControlLog() {
    const r = await api().file_control_status();
    $("#file-control-log").textContent = r.log || "暂无操作记录";
  }

  async function clearFileControlLog() {
    await api().clear_file_control_log();
    renderFileControlLog();
  }

  // ---------- 免责声明 ----------
  function openDisclaimer() {
    $("#disclaimer-modal").classList.remove("hidden");
    $("#disclaimer-agree").checked = false;
    $("#btn-disclaimer-confirm").disabled = true;
  }

  function closeDisclaimer() {
    $("#disclaimer-modal").classList.add("hidden");
    $("#set-file-control").checked = false;
  }

  function onDisclaimerAgree() {
    $("#btn-disclaimer-confirm").disabled = !$("#disclaimer-agree").checked;
  }

  async function confirmDisclaimer() {
    const r = await api().accept_file_control_disclaimer();
    if (r.ok) {
      fillSettings(r.settings);
      $("#disclaimer-modal").classList.add("hidden");
      $("#set-file-control").checked = true;
      showBanner("文件控制已开启", false);
    }
  }

  // ---------- 赞赏码 ----------
  function openDonate() {
    $("#donate-modal").classList.remove("hidden");
  }

  // ---------- 下载模型 ----------
  function openDownload() {
    if (!state.ollamaRunning) {
      showBanner("Ollama 还没启动，先点「启动 Ollama」。", true);
      return;
    }
    const box = $("#model-presets");
    box.innerHTML = "";
    (window.__RECOMMENDED || []).forEach((m) => {
      const el = document.createElement("div");
      el.className = "preset";
      el.innerHTML =
        '<div class="preset-info"><div class="preset-name">' +
        m.name +
        '</div><div class="preset-meta">' +
        m.size +
        " · " +
        m.desc +
        "</div></div>";
      const btn = document.createElement("button");
      btn.textContent = "下载";
      btn.onclick = () => pullModel(m.name, btn);
      el.appendChild(btn);
      box.appendChild(el);
    });
    $("#download-modal").classList.remove("hidden");
  }

  async function pullModel(name, btn) {
    document.querySelectorAll("#model-presets button").forEach((b) => (b.disabled = true));
    const r = await api().pull_model(name);
    if (!r.ok) {
      showBanner(r.error);
      document.querySelectorAll("#model-presets button").forEach((b) => (b.disabled = false));
    }
  }

  function updatePull(ev) {
    const p = $("#pull-progress");
    // 内置模型自动准备触发的下载：自动弹出下载弹窗，让用户看到进度条
    if (ev.auto && $("#download-modal").classList.contains("hidden")) {
      $("#download-modal").classList.remove("hidden");
      $("#model-presets").innerHTML =
        '<div class="preset"><div class="preset-info">' +
        '<div class="preset-name">首次使用：正在准备内置 AI</div>' +
        '<div class="preset-meta">下载基础模型后会自动创建「小墨」与「子成」，完成后窗口自动关闭</div>' +
        "</div></div>";
      hideBanner();
    }
    p.classList.remove("hidden");
    p.querySelector(".pull-name").textContent = "正在下载：" + ev.model;
    p.querySelector(".pull-bar-fill").style.width = (ev.percent || 0) + "%";
    let meta = ev.status;
    if (ev.total) {
      const mb = (ev.completed / 1048576).toFixed(1);
      const total = (ev.total / 1048576).toFixed(1);
      meta += "  " + mb + " / " + total + " MB (" + (ev.percent || 0) + "%)";
    }
    p.querySelector(".pull-meta").textContent = meta;
  }

  function closePull() {
    $("#pull-progress").classList.add("hidden");
    $("#download-modal").classList.add("hidden");
    document.querySelectorAll("#model-presets button").forEach((b) => (b.disabled = false));
  }

  async function cancelPull() {
    await api().cancel_pull();
    closePull();
  }

  // ---------- 设置 ----------
  function openSettings() {
    fillSettings(state.settings);
    $("#settings-panel").classList.remove("hidden");
  }
  async function saveSettings() {
    const fileControlOn = $("#set-file-control").checked;
    const bionicOn = $("#set-bionic").checked;

    // 如果用户想开启文件控制但还没同意免责声明，先弹窗拦截
    if (fileControlOn && !state.fileControlDisclaimerAccepted) {
      $("#set-file-control").checked = false;
      openDisclaimer();
      return;
    }

    const patch = {
      model: $("#set-model").value,
      system_prompt: $("#set-system").value,
      thinking_level: parseInt($("#set-think").value, 10),
      temperature: parseFloat($("#set-temp").value),
      top_p: parseFloat($("#set-topp").value),
      num_ctx: parseInt($("#set-ctx").value, 10),
      keep_alive: $("#set-keep").value,
      bionic_enabled: bionicOn,
      file_control_enabled: fileControlOn,
    };
    const r = await api().save_settings(patch);
    if (r.ok) {
      fillSettings(r.settings);
      $("#model-select").value = r.settings.model;
      $("#model-mini").textContent = "模型：" + (r.settings.model || "未选择");
    }
    $("#settings-panel").classList.add("hidden");
  }

  function updateFileControlStatus() {
    const el = $("#file-control-status");
    if (!el) return;
    if (state.fileControlEnabled) {
      el.textContent = "当前状态：已开启（AI 可在您确认后操作文件）";
      el.style.color = "var(--green)";
    } else {
      el.textContent = "当前状态：关闭";
      el.style.color = "var(--red)";
    }
  }

  function applyBionicClass() {
    document.body.classList.toggle("bionic", state.bionicEnabled);
  }

  // ---------- 知识库 ----------
  function openKb() {
    $("#kb-enable").checked = !!state.settings.knowledge_enabled;
    api().list_documents().then((r) => {
      if (r.ok) renderKbList(r.documents || []);
    });
    $("#kb-panel").classList.remove("hidden");
  }

  function renderKbList(docs) {
    const box = $("#kb-list");
    box.innerHTML = "";
    if (!docs.length) {
      box.innerHTML = '<div class="kb-empty">还没有导入任何资料。</div>';
      return;
    }
    docs.forEach((d) => {
      const el = document.createElement("div");
      el.className = "kb-item";
      const info = document.createElement("div");
      info.className = "kb-item-info";
      const nm = document.createElement("div");
      nm.className = "kb-item-name";
      nm.textContent = d.name;
      const meta = document.createElement("div");
      meta.className = "kb-item-meta";
      meta.textContent = (d.kind || "text") + " · " + (d.chunks || 0) + " 块";
      info.appendChild(nm);
      info.appendChild(meta);
      const del = document.createElement("button");
      del.className = "kb-del";
      del.textContent = "删除";
      del.onclick = () => deleteKb(d.id);
      const sum = document.createElement("button");
      sum.className = "kb-sum";
      sum.textContent = "总结";
      sum.onclick = () => summarizeDoc(d.id, sum);
      el.appendChild(info);
      el.appendChild(del);
      el.appendChild(sum);
      box.appendChild(el);
    });
  }

  async function pickKb() {
    const btn = $("#kb-pick");
    btn.disabled = true;
    btn.textContent = "导入中…";
    const r = await api().import_documents_dialog();
    btn.disabled = false;
    btn.textContent = "选择文件导入";
    if (!r.ok) {
      showBanner("导入失败：" + (r.error || ""));
    } else if (r.error) {
      showBanner("部分导入失败：" + r.error);
    }
    if (r.documents) renderKbList(r.documents);
  }

  async function deleteKb(id) {
    const r = await api().delete_document(id);
    if (r.ok) renderKbList(r.documents || []);
  }

  function toggleKb() {
    const on = $("#kb-enable").checked;
    state.settings.knowledge_enabled = on;
    api().save_settings({ knowledge_enabled: on });
  }

  // ---------- 文件工具：表格分析 ----------
  async function importSheet() {
    const btn = $("#sheet-pick");
    btn.disabled = true;
    btn.textContent = "读取中…";
    const r = await api().import_sheet_dialog();
    btn.disabled = false;
    btn.textContent = "导入表格 (CSV / XLSX)";
    if (!r.ok) {
      showBanner("导入失败：" + (r.error || ""));
      return;
    }
    if (r.cancelled) return;
    state.currentSheetId = r.sheet_id;
    renderSheet(r);
  }

  function renderSheet(d) {
    $("#sheet-info").classList.remove("hidden");
    $("#sheet-ask").classList.remove("hidden");
    $("#sheet-info").innerHTML =
      "<b>" + MD.escapeHtml(d.name) + "</b> · " + d.n_rows + " 行 × " + d.n_cols + " 列";
    let stats =
      "<table class='sheet-stats'><thead><tr><th>列</th><th>类型</th><th>非空</th><th>空</th></tr></thead><tbody>";
    d.stats.forEach((s) => {
      stats +=
        "<tr><td>" + MD.escapeHtml(s.name) + "</td><td>" + s.type + "</td><td>" +
        s.non_null + "</td><td>" + s.nulls + "</td></tr>";
    });
    stats += "</tbody></table>";
    let prev = "<table class='sheet-prev'><thead><tr>";
    d.headers.forEach((h) => (prev += "<th>" + MD.escapeHtml(h) + "</th>"));
    prev += "</tr></thead><tbody>";
    d.preview.forEach((row) => {
      prev += "<tr>";
      d.headers.forEach((h) => (prev += "<td>" + MD.escapeHtml(String(row[h] ?? "")) + "</td>"));
      prev += "</tr>";
    });
    prev += "</tbody></table>";
    $("#sheet-preview").classList.remove("hidden");
    $("#sheet-preview").innerHTML = stats + prev;
    $("#sheet-result").classList.add("hidden");
  }

  async function askSheet() {
    const q = $("#sheet-q").value.trim();
    if (!q || state.sheetGenerating) return;
    const r = await api().analyze_sheet(q);
    if (!r.ok) {
      showBanner(r.error || "分析失败");
    }
  }

  // ---------- 文件工具：文件整理 ----------
  async function pickFolder() {
    const r = await api().pick_folder_dialog();
    if (!r.ok) {
      showBanner("打开失败：" + (r.error || ""));
      return;
    }
    if (r.cancelled) return;
    await scanFolder(r.path);
  }

  async function scanFolder(path) {
    const r = await api().automation_scan(path);
    if (!r.ok) {
      showBanner("扫描失败：" + (r.error || ""));
      return;
    }
    state.autoPath = path;
    $("#auto-path").textContent = "📂 " + path;
    const items = Object.entries(r.by_ext).sort((a, b) => b[1] - a[1]);
    const html = "共 " + r.count + " 个文件。扩展名分布：" +
      items.map((e) => e[0] + " ×" + e[1]).join("，");
    $("#auto-summary").classList.remove("hidden");
    $("#auto-summary").textContent = html;
    $("#auto-actions").classList.remove("hidden");
  }

  async function archiveFolder() {
    if (!state.autoPath) return;
    if (!confirm("将按类型把文件移入对应子文件夹（「其他」类保持原位）。确定？")) return;
    const r = await api().automation_archive(state.autoPath);
    if (r.ok) {
      showBanner("已归档 " + (r.done || 0) + " 个文件。" +
        (r.errors && r.errors.length ? "部分失败：" + r.errors.join("; ") : ""));
      await scanFolder(state.autoPath);
    } else {
      showBanner("归档失败：" + (r.error || ""));
    }
  }

  async function previewRename() {
    if (!state.autoPath) {
      showBanner("请先选择文件夹。");
      return;
    }
    const mode = $("#rename-mode").value;
    const params = {};
    if (mode === "sequence") {
      params.prefix = $("#rename-prefix").value || "file";
      params.ext = $("#rename-ext").value.trim();
      params.start = parseInt($("#rename-start").value, 10) || 1;
    } else {
      params.old = $("#rename-old").value;
      params.new = $("#rename-new").value;
    }
    const r = await api().automation_preview_rename(state.autoPath, mode, params);
    if (!r.ok) {
      showBanner("预览失败：" + (r.error || ""));
      return;
    }
    state.renamePlan = r.renames;
    let html = "将重命名 " + r.count + " 个文件：<ul class='rename-list'>";
    r.renames.slice(0, 50).forEach((x) => {
      html += "<li>" + MD.escapeHtml(basename(x.src)) + " → " + MD.escapeHtml(basename(x.dst)) + "</li>";
    });
    if (r.renames.length > 50) html += "<li>…共 " + r.renames.length + " 项</li>";
    html += "</ul>";
    $("#rename-plan").classList.remove("hidden");
    $("#rename-plan").innerHTML = html;
    $("#rename-apply").classList.toggle("hidden", r.count === 0);
  }

  async function applyRename() {
    if (!state.renamePlan || !state.renamePlan.length) return;
    if (!confirm("确认执行上述 " + state.renamePlan.length + " 项重命名？")) return;
    const r = await api().automation_apply_rename(state.autoPath, state.renamePlan);
    if (r.ok) {
      showBanner("已重命名 " + (r.done || 0) + " 个文件。" +
        (r.errors && r.errors.length ? "失败：" + r.errors.join("; ") : ""));
      $("#rename-plan").classList.add("hidden");
      $("#rename-apply").classList.add("hidden");
      state.renamePlan = null;
      await scanFolder(state.autoPath);
    } else {
      showBanner("重命名失败：" + (r.errors ? r.errors.join("; ") : r.error || ""));
    }
  }

  // ---------- 文档摘要 ----------
  let _summaryBtn = null;
  async function summarizeDoc(docId, btn) {
    _summaryBtn = btn || null;
    if (_summaryBtn) _summaryBtn.disabled = true;
    showBanner("正在生成摘要…", false);
    const r = await api().summarize_document(docId);
    if (!r.ok) {
      if (_summaryBtn) { _summaryBtn.disabled = false; _summaryBtn = null; }
      hideBanner();
      showBanner("摘要失败：" + (r.error || ""));
    }
    // 成功时结果通过 doc_summary 事件异步返回，这里不阻塞等待
  }

  function basename(p) {
    return String(p).split(/[\\/]/).pop();
  }

  // ---------- 绑定 ----------
  function bindUI() {
    $("#btn-new").onclick = async () => {
      const r = await api().new_session("新对话");
      state.currentId = r.session.id;
      renderSessions(r.sessions);
      $("#messages").innerHTML = "";
      $("#session-title").textContent = "新对话";
    };
    $("#btn-send").onclick = send;
    $("#btn-stop").onclick = stop;
    $("#btn-download").onclick = openDownload;
    $("#btn-kb").onclick = openKb;
    $("#btn-tools").onclick = () => $("#tools-panel").classList.remove("hidden");
    $("#btn-file-control").onclick = openFileControlPanel;
    $("#btn-settings").onclick = openSettings;
    $("#btn-data").onclick = () => api().open_data_dir();
    $("#btn-donate").onclick = openDonate;
    $("#kb-pick").onclick = pickKb;
    $("#kb-enable").onchange = toggleKb;

    // 文件控制 / 免责声明 / 赞赏码
    $("#btn-goto-file-settings").onclick = () => {
      $("#file-control-panel").classList.add("hidden");
      openSettings();
    };
    $("#btn-clear-file-log").onclick = clearFileControlLog;
    $("#disclaimer-agree").onchange = onDisclaimerAgree;
    $("#btn-disclaimer-cancel").onclick = closeDisclaimer;
    $("#btn-disclaimer-confirm").onclick = confirmDisclaimer;
    $("#btn-file-op-cancel").onclick = cancelFileOp;
    $("#btn-file-op-confirm").onclick = confirmFileOp;

    // 文件工具
    document.querySelectorAll(".tools-tab").forEach((b) => {
      b.onclick = () => {
        document.querySelectorAll(".tools-tab").forEach((t) => t.classList.toggle("active", t === b));
        document.querySelectorAll(".tools-section").forEach((s) =>
          s.classList.toggle("hidden", s.dataset.section !== b.dataset.tab)
        );
      };
    });
    $("#sheet-pick").onclick = importSheet;
    $("#sheet-send").onclick = askSheet;
    $("#auto-pick").onclick = pickFolder;
    $("#auto-archive").onclick = archiveFolder;
    $("#rename-mode").onchange = () => {
      const mode = $("#rename-mode").value;
      $("#rename-seq").classList.toggle("hidden", mode !== "sequence");
      $("#rename-rep").classList.toggle("hidden", mode !== "replace");
    };
    $("#rename-preview").onclick = previewRename;
    $("#rename-apply").onclick = applyRename;
    $("#btn-save-settings").onclick = saveSettings;
    $("#btn-cancel-pull").onclick = cancelPull;
    $("#model-select").onchange = () => {
      const v = $("#model-select").value;
      api().save_settings({ model: v });
      $("#set-model").value = v;
      $("#model-mini").textContent = "模型：" + (v || "未选择");
    };

    // 关闭弹窗
    document.querySelectorAll("[data-close]").forEach((b) => {
      b.onclick = () => {
        const modal = b.closest(".modal");
        if (modal && modal.id === "file-op-modal") {
          cancelFileOp();
          return;
        }
        document.querySelectorAll(".modal").forEach((m) => m.classList.add("hidden"));
      };
    });
    document.querySelectorAll("[data-close-panel]").forEach((b) => {
      b.onclick = () =>
        document.querySelectorAll(".panel").forEach((p) => p.classList.add("hidden"));
    });
    $("#download-modal").onclick = (e) => {
      if (e.target.id === "download-modal") $("#download-modal").classList.add("hidden");
    };

    // 滑块联动
    $("#set-temp").oninput = (e) => ($("#val-temp").textContent = e.target.value);
    $("#set-topp").oninput = (e) => ($("#val-topp").textContent = e.target.value);

    // Bionic Reading 实时预览
    $("#set-bionic").onchange = () => {
      state.bionicEnabled = $("#set-bionic").checked;
      applyBionicClass();
      document.querySelectorAll(".bubble.assistant").forEach((b) => applyBionicToElement(b));
    };

    // 恢复默认设置
    $("#btn-reset-settings").onclick = async () => {
      if (!confirm("确定恢复默认设置吗？当前人设、参数等将重置。")) return;
      const r = await api().reset_settings();
      if (r.ok) fillSettings(r.settings);
    };

    // 输入：Enter 发送，Shift+Enter 换行
    const ta = $("#input");
    ta.addEventListener("input", autoResize);
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });

    // 复制按钮（事件委托）
    document.addEventListener("click", (e) => {
      const btn = e.target.closest(".copy-btn");
      if (!btn) return;
      const code = decodeURIComponent(btn.dataset.code || "");
      const done = () => {
        btn.textContent = "已复制";
        btn.classList.add("done");
        setTimeout(() => {
          btn.textContent = "复制";
          btn.classList.remove("done");
        }, 1200);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(code).then(done).catch(() => fallbackCopy(code, done));
      } else {
        fallbackCopy(code, done);
      }
    });
  }

  function fallbackCopy(text, done) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
      done();
    } catch (e) {}
    document.body.removeChild(ta);
  }

  // ---------- 启动 ----------
  function whenApi(cb) {
    if (window.pywebview && window.pywebview.api) cb();
    else window.addEventListener("pywebviewready", cb, { once: true });
  }

  async function boot() {
    bindUI();
    whenApi(async () => {
      // bootstrap() 现在只读本地数据，Ollama 状态由后台事件 ollama_status 更新，
      // 避免 Ollama 未就绪时阻塞前端导致窗口「未响应」。
      const r = await api().bootstrap();
      window.__RECOMMENDED = r.recommended || [];
      state.ollamaRunning = r.ollama_running;
      fillSettings(r.settings);
      renderModels(r.models);
      renderSessions(r.sessions);
      setOllamaStatus(r.ollama_running, r.ollama_version);
      if (!r.ollama_running) {
        showBanner("Ollama 未连接，点击左下角状态或「启动 Ollama」按钮启动。");
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
