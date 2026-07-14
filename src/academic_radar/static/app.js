(() => {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const toast = document.getElementById("toast");
  const announce = (message, error = false) => {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.toggle("error", error);
    toast.hidden = false;
    clearTimeout(announce.timer);
    announce.timer = setTimeout(() => { toast.hidden = true; }, 4200);
  };
  const api = async (url, options = {}) => {
    const response = await fetch(url, {
      ...options,
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf, ...(options.headers || {})},
    });
    const data = await response.json().catch(() => ({error: "服务器返回了无法识别的响应"}));
    if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
    return data;
  };

  document.querySelectorAll(".paper-details").forEach((details) => {
    const summary = details.querySelector(":scope > summary");
    details.addEventListener("toggle", () => {
      if (summary) summary.textContent = details.open ? "收起详情" : "展开详情";
      if (details.open) requestAnimationFrame(setupAbstracts);
    });
  });
  function setupAbstracts() {
    document.querySelectorAll("[data-abstract-wrap]").forEach((wrap) => {
      const text = wrap.querySelector("[data-abstract]");
      const button = wrap.querySelector("[data-abstract-toggle]");
      if (!text || !button || wrap.dataset.ready) return;
      wrap.dataset.ready = "1";
      text.classList.add("is-collapsed");
      const overflowed = text.scrollHeight > text.clientHeight + 2;
      button.hidden = !overflowed;
      if (!overflowed) text.classList.remove("is-collapsed");
      button.addEventListener("click", () => {
        const expanded = button.getAttribute("aria-expanded") === "true";
        button.setAttribute("aria-expanded", String(!expanded));
        button.textContent = expanded ? "展开摘要" : "收起摘要";
        text.classList.toggle("is-collapsed", expanded);
      });
    });
  }
  setupAbstracts();

  document.addEventListener("click", async (event) => {
    const favorite = event.target.closest("[data-favorite]");
    if (favorite) {
      event.preventDefault();
      const next = favorite.getAttribute("aria-pressed") !== "true";
      favorite.disabled = true;
      try {
        const result = await api("/api/favorite", {method: "POST", body: JSON.stringify({identity: favorite.dataset.identity, favorite: next})});
        favorite.setAttribute("aria-pressed", String(result.favorite));
        favorite.classList.toggle("is-favorite", result.favorite);
        favorite.textContent = result.favorite ? "★" : "☆";
        favorite.setAttribute("aria-label", result.favorite ? "取消收藏" : "收藏到本地文献库");
        announce(result.message);
      } catch (error) { announce(error.message, true); }
      finally { favorite.disabled = false; }
    }
  });

  const pdfDialog = document.getElementById("pdf-dialog");
  const openPdf = (identity = "") => {
    if (!pdfDialog) {
      location.href = "/library?pdf=" + encodeURIComponent(identity);
      return;
    }
    const select = pdfDialog.querySelector("[data-paper-select]");
    if (identity && select) select.value = identity;
    pdfDialog.showModal();
  };
  document.querySelectorAll("[data-open-pdf]").forEach((button) => button.addEventListener("click", () => openPdf(button.dataset.identity || "")));
  const requestedPdf = new URLSearchParams(location.search).get("pdf");
  if (requestedPdf && pdfDialog) openPdf(requestedPdf);
  document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => button.closest("dialog")?.close()));
  document.querySelector("[data-paper-search]")?.addEventListener("input", (event) => {
    const query = event.target.value.trim().toLocaleLowerCase();
    document.querySelectorAll("[data-paper-select] option").forEach((option) => {
      option.hidden = Boolean(option.value && query && !option.textContent.toLocaleLowerCase().includes(query));
    });
  });
  document.querySelector("[data-pdf-file]")?.addEventListener("change", (event) => {
    const file = event.target.files?.[0];
    const summary = document.querySelector("[data-file-summary]");
    if (summary) summary.textContent = file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB` : "尚未选择文件";
  });

  const sourceRoot = document.querySelector("[data-source-search]");
  if (sourceRoot) {
    const input = sourceRoot.querySelector("#source-query");
    const list = sourceRoot.querySelector("[data-source-status]")?.nextElementSibling;
    const status = sourceRoot.querySelector("[data-source-status]");
    const spinner = sourceRoot.querySelector("[data-source-spinner]");
    let timer = 0, active = -1, controller = null, previewToken = "";
    const options = () => [...list.querySelectorAll('[role="option"]:not([aria-disabled="true"])')];
    const selectIndex = (index) => {
      const items = options();
      active = items.length ? (index + items.length) % items.length : -1;
      items.forEach((item, i) => {
        item.classList.toggle("active", i === active);
        item.setAttribute("aria-selected", String(i === active));
      });
      if (active >= 0) {
        input.setAttribute("aria-activedescendant", items[active].id);
        items[active].scrollIntoView({block: "nearest"});
      } else input.removeAttribute("aria-activedescendant");
    };
    const preview = async (id) => {
      status.textContent = "正在读取真实作品预览…";
      try {
        const result = await api("/api/sources/preview", {method: "POST", body: JSON.stringify({candidate_id: id})});
        previewToken = result.token;
        const dialog = document.getElementById("source-preview-dialog");
        dialog.querySelector("[data-preview-title]").textContent = `确认添加“${result.source.name}”`;
        dialog.querySelector("[data-preview-summary]").textContent = `从 ${result.since} 起找到 ${result.total_results} 个结果。添加前请核对样例。`;
        const samples = dialog.querySelector("[data-preview-list]");
        samples.replaceChildren(...(result.samples.length ? result.samples : [{title: "当前时间窗没有样例", venue: "可稍后重试"}]).map((item) => {
          const li = document.createElement("li");
          const strong = document.createElement("strong"); strong.textContent = item.title;
          const span = document.createElement("span"); span.textContent = [item.venue, item.doi].filter(Boolean).join(" · ");
          li.append(strong, span); return li;
        }));
        dialog.showModal(); status.textContent = "预览已就绪，确认后才会保存。";
      } catch (error) { status.textContent = error.message; announce(error.message, true); }
    };
    const render = (items, message) => {
      list.replaceChildren();
      items.forEach((item, index) => {
        const button = document.createElement("button");
        button.type = "button"; button.role = "option"; button.id = `source-option-${index}`;
        button.dataset.id = item.candidate_id; button.setAttribute("aria-selected", "false");
        button.setAttribute("aria-disabled", String(Boolean(item.added))); button.disabled = Boolean(item.added);
        const title = document.createElement("strong"); title.textContent = item.name + (item.added ? "（已添加）" : "");
        const meta = document.createElement("span");
        meta.textContent = [item.source_type, item.publisher, item.issn && `ISSN ${item.issn}`, item.openalex_id && `OpenAlex ${item.openalex_id}`, item.match_basis].filter(Boolean).join(" · ");
        button.append(title, meta); button.addEventListener("click", () => preview(item.candidate_id)); list.append(button);
      });
      list.hidden = items.length === 0; input.setAttribute("aria-expanded", String(items.length > 0));
      status.textContent = message || (items.length ? `找到 ${items.length} 个候选` : "没有找到可验证的来源");
      active = -1; input.removeAttribute("aria-activedescendant");
    };
    input.addEventListener("input", () => {
      clearTimeout(timer); controller?.abort();
      const query = input.value.trim();
      if (query.length < 2) { render([], "请输入至少 2 个字符"); return; }
      spinner.hidden = false; status.textContent = "搜索中…";
      timer = setTimeout(async () => {
        controller = new AbortController();
        try {
          const response = await fetch(`/api/sources/search?q=${encodeURIComponent(query)}`, {signal: controller.signal});
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || "搜索失败");
          render(data.items || [], data.message);
        } catch (error) {
          if (error.name !== "AbortError") render([], `${error.message}。请稍后重试。`);
        } finally { spinner.hidden = true; }
      }, 320);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") { event.preventDefault(); selectIndex(active + 1); }
      else if (event.key === "ArrowUp") { event.preventDefault(); selectIndex(active - 1); }
      else if (event.key === "Enter" && active >= 0) { event.preventDefault(); options()[active]?.click(); }
      else if (event.key === "Escape") {
        list.hidden = true; active = -1; input.setAttribute("aria-expanded", "false");
        input.removeAttribute("aria-activedescendant");
      }
    });
    document.querySelector("[data-confirm-source]")?.addEventListener("click", async (event) => {
      event.target.disabled = true;
      try {
        const result = await api("/api/sources/confirm", {method: "POST", body: JSON.stringify({token: previewToken})});
        announce(result.message); location.reload();
      } catch (error) { announce(error.message, true); event.target.disabled = false; }
    });
    document.querySelectorAll("[data-remove-source]").forEach((button) => button.addEventListener("click", async () => {
      const accepted = confirm(`移除“${button.dataset.name}”将停止未来监测。历史论文默认保留，也不会删除收藏、反馈或 PDF。继续吗？`);
      if (!accepted) return;
      button.disabled = true;
      try {
        const result = await api("/api/sources/remove", {method: "POST", body: JSON.stringify({name: button.dataset.name})});
        announce(result.message); location.reload();
      } catch (error) { announce(error.message, true); button.disabled = false; }
    }));
  }

  const taskProgress = document.querySelector("[data-task-progress]");
  const pollTask = async (taskId) => {
    taskProgress.hidden = false;
    try {
      const task = await api(`/api/tasks/${encodeURIComponent(taskId)}`, {headers: {"Content-Type": "application/json"}});
      const total = task.total_count || task.details?.checked || 1;
      const completed = task.completed_count || 0;
      taskProgress.querySelector("[data-task-progressbar]").max = total;
      taskProgress.querySelector("[data-task-progressbar]").value = completed;
      taskProgress.querySelector("[data-task-message]").textContent = `${task.message || "处理中"}${total > 1 ? `（${completed}/${total}）` : ""}`;
      if (task.status === "running") return setTimeout(() => pollTask(taskId), 1000);
      const failed = task.status === "failed" || task.status === "partial";
      taskProgress.querySelector("[data-task-retry]").hidden = !failed;
      announce(task.message || "任务完成", task.status === "failed");
      if (!failed) setTimeout(() => location.reload(), 800);
    } catch (error) { announce(error.message, true); }
  };
  document.querySelectorAll("[data-task-action]").forEach((button) => button.addEventListener("click", async () => {
    const action = button.dataset.taskAction;
    if (["enrich", "export"].includes(action) && !confirm(action === "enrich" ? "现在开始从官方元数据渠道补全摘要吗？已有更长摘要不会被覆盖。" : "现在采集新论文并建立 Codex 判断队列吗？")) return;
    button.disabled = true;
    try {
      if (action === "recheck") {
        const result = await api("/api/tasks/recheck", {method: "POST", body: "{}"});
        announce(result.ok ? "状态检查完成" : "检查发现需要处理的项目"); setTimeout(() => location.reload(), 500);
      } else if (action === "package") {
        const result = await api("/api/tasks/missing-package", {method: "POST", body: "{}"});
        announce(`已导出 ${result.count} 项：${result.output}`);
      } else {
        const result = await api(`/api/tasks/${action}`, {method: "POST", body: JSON.stringify({})});
        await pollTask(result.task_id);
      }
    } catch (error) { announce(error.message, true); }
    finally { button.disabled = false; }
  }));
  document.querySelector("[data-task-retry]")?.addEventListener("click", async () => {
    try {
      const result = await api("/api/tasks/enrich", {method: "POST", body: JSON.stringify({retry: true})});
      await pollTask(result.task_id);
    } catch (error) { announce(error.message, true); }
  });
  document.querySelector("[data-copy]")?.addEventListener("click", async () => {
    const text = document.querySelector("[data-copy-target]")?.value || "";
    try { await navigator.clipboard.writeText(text); announce("任务提示词已复制"); }
    catch (_) { announce("无法访问剪贴板，请手动复制", true); }
  });
})();
