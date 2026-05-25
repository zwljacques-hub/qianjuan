// === BEGIN qianjuan: base path + fetch interceptor + auth token ===
// 让前端在 "/" 根路径(本地 dev)和 "/toolbox/qianjuan/" 子路径(生产)下都能工作
// 同时为 /api/* 自动挂上 AI 秘密基地登录态:
//   优先 1. Authorization: Bearer <ec_ai_token>  (老路径, 主站签发的 JWT)
//   优先 2. X-Forum-Current-User: base64(JSON)   (新路径, 同源 localStorage trust)
(function () {
  const pathMatch = window.location.pathname.match(/^(\/toolbox\/[^/]+)\//);
  window.QJ_BASE = pathMatch ? pathMatch[1] : '';
  window.QJ_LOGIN_URL = 'https://www.aisecretlair.com/';
  window.QJ_TOKEN_KEY = 'ec_ai_token';
  window.QJ_FORUM_USER_KEY = 'aisecretlair-forum-current-user';
  window.qjGetAuthToken = function () {
    try { return window.localStorage.getItem(window.QJ_TOKEN_KEY) || ''; } catch (_) { return ''; }
  };
  window.qjClearAuthToken = function () {
    try { window.localStorage.removeItem(window.QJ_TOKEN_KEY); } catch (_) {}
  };
  // 读 ai秘密基地主站 forum 当前用户 (同源 localStorage), base64-url 编码成 header 值
  window.qjGetForumUserHeader = function () {
    try {
      const raw = window.localStorage.getItem(window.QJ_FORUM_USER_KEY);
      if (!raw) return '';
      // 校验是合法 JSON 且有 id, 防止旧脏数据
      const u = JSON.parse(raw);
      if (!u || !u.id) return '';
      // base64url (utf-8 安全): 先 encodeURIComponent 防中文 btoa 报错
      const utf8 = unescape(encodeURIComponent(JSON.stringify({
        id: String(u.id),
        email: u.email || '',
        nickname: u.nickname || '',
      })));
      return btoa(utf8).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    } catch (_) { return ''; }
  };
  window.qjHasForumUser = function () {
    return !!window.qjGetForumUserHeader();
  };
  const _origFetch = window.fetch.bind(window);
  window._origRawFetch = _origFetch;  // 留给诊断面板:绕过千卷子路径前缀
  window.fetch = function (input, init) {
    const isApi = typeof input === 'string' && input.startsWith('/api/');
    if (isApi) {
      const opts = Object.assign({}, init || {});
      const headers = new Headers(opts.headers || {});
      const token = window.qjGetAuthToken();
      if (token && !headers.has('Authorization')) {
        headers.set('Authorization', 'Bearer ' + token);
      }
      // 同源 localStorage trust 通道
      const forumHdr = window.qjGetForumUserHeader();
      if (forumHdr && !headers.has('X-Forum-Current-User')) {
        headers.set('X-Forum-Current-User', forumHdr);
      }
      opts.headers = headers;
      return _origFetch(window.QJ_BASE + input, opts).then(function (resp) {
        // 401 → 清掉过期 token 并触发遮罩
        if (resp.status === 401) {
          window.qjClearAuthToken();
          if (typeof window.qjRenderAuthGate === 'function') {
            try { window.qjRenderAuthGate(true); } catch (_) {}
          }
        }
        return resp;
      });
    }
    return _origFetch(input, init);
  };
})();
// === END qianjuan ===

const views = document.querySelectorAll(".view-panel");
const navItems = document.querySelectorAll(".nav-item");
const toast = document.getElementById("toast");

const API = {
  async getState() {
    const response = await fetch("/api/state");
    return parseStateResponse(response);
  },
  async getLLMStatus() {
    const response = await fetch("/api/llm/status");
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    return payload.llm;
  },
  async getLLMConfig() {
    const response = await fetch("/api/llm/config");
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    return payload.llm;
  },
  async getProjects() {
    const response = await fetch("/api/projects");
    const payload = await response.json();
    if (!response.ok || payload.error) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    return payload.projects || [];
  },
  async post(path, body = {}) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return parseStateResponse(response);
  },
};

let appState = null;
let llmStatus = null;
let llmConfig = null;
let projectList = [];
let selectedSceneId = null;
let selectedMemory = "canon";
let selectedCharacter = "suHan";
let editorDirty = false;

async function parseStateResponse(response) {
  const payload = await response.json();
  if (!response.ok || payload.error) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2200);
}

// 走 fetch(自动带子路径前缀);
// 现代浏览器(Chrome/Edge)用 showSaveFilePicker 弹原生保存对话框,默认定位桌面;
// Firefox/Safari 等不支持的浏览器自动回退到普通 Blob 下载(浏览器默认下载文件夹)。
async function downloadExport(path) {
  try {
    const response = await fetch(path);
    if (!response.ok) {
      let message = `导出失败（HTTP ${response.status}）`;
      try {
        const data = await response.json();
        if (data && data.error) message = data.error;
      } catch (_) {}
      showToast(message);
      return;
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename\*?="?([^";]+)"?/i);
    const filename = match ? decodeURIComponent(match[1].replace(/^UTF-8''/i, "")) : "export.txt";

    // 优先用 File System Access API:能弹原生保存对话框,startIn:"desktop" 让对话框默认定位桌面
    if (typeof window.showSaveFilePicker === "function") {
      try {
        const handle = await window.showSaveFilePicker({
          suggestedName: filename,
          startIn: "desktop",
          types: [
            {
              description: "文本文档",
              accept: { "text/plain": [".txt"] },
            },
          ],
        });
        const writable = await handle.createWritable();
        await writable.write(blob);
        await writable.close();
        showToast(`已保存到桌面：${handle.name || filename}`);
        return;
      } catch (pickerErr) {
        // 用户取消保存对话框时不报错,静默退出
        if (pickerErr && pickerErr.name === "AbortError") return;
        // 其他异常(如权限被拒)走回退
        console.warn("showSaveFilePicker 失败，回退到普通下载", pickerErr);
      }
    }

    // 回退:普通 Blob 下载,落到浏览器默认下载文件夹
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast(`已下载 ${filename}（保存在浏览器默认下载位置）`);
  } catch (error) {
    showToast(`导出失败：${error.message || error}`);
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatDateTime(value) {
  if (!value) return "暂无时间";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatWords(value) {
  const number = Number(value || 0);
  if (!number) return "未设置";
  if (number >= 10000) return `${Math.round(number / 10000)}万字`;
  return `${number}字`;
}

function setButtonBusy(button, busy, busyText = "处理中...") {
  if (!button) return;
  if (busy) {
    button.dataset.defaultText = button.dataset.defaultText || button.textContent;
    button.textContent = busyText;
    button.disabled = true;
    return;
  }
  button.textContent = button.dataset.defaultText || button.textContent;
  button.disabled = false;
}

function switchView(viewName) {
  views.forEach((view) => view.classList.toggle("is-visible", view.id === `view-${viewName}`));
  navItems.forEach((item) => item.classList.toggle("active", item.dataset.view === viewName));
}

function openNewProjectModal() {
  const modal = document.getElementById("newProjectModal");
  modal.classList.add("is-open");
  modal.setAttribute("aria-hidden", "false");
}

function closeNewProjectModal() {
  const modal = document.getElementById("newProjectModal");
  modal.classList.remove("is-open");
  modal.setAttribute("aria-hidden", "true");
}

function openIdeaModal() {
  const modal = document.getElementById("ideaProjectModal");
  modal.classList.add("is-open");
  modal.setAttribute("aria-hidden", "false");
}

function closeIdeaModal() {
  const modal = document.getElementById("ideaProjectModal");
  modal.classList.remove("is-open");
  modal.setAttribute("aria-hidden", "true");
}

function openProjectShelfModal() {
  const modal = document.getElementById("projectShelfModal");
  modal.classList.add("is-open");
  modal.setAttribute("aria-hidden", "false");
}

function closeProjectShelfModal() {
  const modal = document.getElementById("projectShelfModal");
  modal.classList.remove("is-open");
  modal.setAttribute("aria-hidden", "true");
}

function closeChapterReaderModal() {
  const modal = document.getElementById("chapterReaderModal");
  modal.classList.remove("is-open");
  modal.setAttribute("aria-hidden", "true");
}

function resetWorkspaceSelection(state = appState) {
  selectedSceneId = state?.scenes?.[0]?.id || null;
  selectedMemory = "canon";
  selectedCharacter = "suHan";
  editorDirty = false;
  document.querySelectorAll(".segment").forEach((item) => {
    item.classList.toggle("active", item.dataset.memory === selectedMemory);
  });
}

async function mutate(path, body, fallbackMessage) {
  try {
    const payload = await API.post(path, body);
    appState = payload.state;
    renderAll();
    showToast(payload.message || fallbackMessage);
    return payload;
  } catch (error) {
    showToast(`操作失败：${error.message}`);
    return null;
  }
}

function currentScene() {
  if (!appState?.scenes?.length) return null;
  return appState.scenes.find((scene) => scene.id === selectedSceneId) || appState.scenes[0];
}

function normalizeTags(tags) {
  if (Array.isArray(tags)) return tags;
  if (typeof tags === "string") {
    return tags
      .split(/[,\s，、/]+/)
      .map((tag) => tag.trim())
      .filter(Boolean);
  }
  return [];
}

function updateSaveButton() {
  const button = document.getElementById("saveSceneBtn");
  if (!button) return;
  button.textContent = editorDirty ? "保存修改 *" : "保存修改";
  button.disabled = !currentScene();
}

function markEditorDirty() {
  editorDirty = true;
  updateSaveButton();
}

function updateHeader() {
  const project = appState.project;
  const title = document.querySelector(".topbar h1");
  const eyebrow = document.querySelector(".topbar .eyebrow");
  const projectName = document.querySelector(".project-button span:first-child");
  title.textContent = `第 ${project.chapterNumber} 章 · ${project.chapterTitle}`;
  eyebrow.textContent = `工作流版本 ${project.workflowVersion}`;
  projectName.textContent = project.title;
}

function renderChapterList() {
  const list = document.querySelector(".chapter-list");
  list.innerHTML = '<div class="section-kicker">章节</div>';
  appState.chapters.forEach((chapter) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `chapter-row ${chapter.status}`;
    button.innerHTML = `<span>${chapter.number}</span><span>${chapter.title}</span>`;
    button.addEventListener("click", () => {
      if (chapter.status === "done") {
        switchView("book");
        showToast(`已在作品总览中打开第 ${chapter.number} 章归档`);
        return;
      }
      showToast(`已选择第 ${chapter.number} 章：${chapter.title}`);
    });
    list.appendChild(button);
  });
}

function renderWorkspace() {
  document.getElementById("directorDecision").textContent = appState.directorDecision;
  document.getElementById("draftScore").textContent = `${appState.draftScore} / 100`;

  const statusTiles = document.querySelectorAll(".status-tile strong");
  if (statusTiles[1]) statusTiles[1].textContent = appState.project.arcName;

  const planBlocks = document.querySelectorAll(".plan-block strong");
  if (planBlocks[0]) planBlocks[0].textContent = appState.plan.readerPromise;
  if (planBlocks[1]) planBlocks[1].textContent = appState.plan.mustAdvance;
  if (planBlocks[2]) planBlocks[2].textContent = appState.plan.mustNotResolve;

  renderScenes();
  renderEditor();
  renderPatches();
}

function renderQuickStart() {
  const model = document.getElementById("quickModelStatus");
  if (model) {
    const configured = Boolean(llmStatus?.configured);
    model.textContent = configured ? `大模型已连接：${llmStatus.model}` : "未配置大模型，将使用本地生成";
    model.className = `model-status ${configured ? "connected" : "offline"}`;
  }

  const currentTitle = document.getElementById("quickCurrentTitle");
  const currentStep = document.getElementById("quickCurrentStep");
  if (currentTitle) currentTitle.textContent = `当前作品：《${appState.project.title}》`;
  if (currentStep) currentStep.textContent = `推荐下一步：${appState.workflow?.primaryLabel || "等待操作"}`;

  const finishPanel = document.getElementById("finishPanel");
  if (finishPanel) {
    finishPanel.hidden = appState.workflow?.currentStep !== "export";
  }
}

function currentProjectSummary() {
  const project = appState?.project || {};
  return {
    id: project.id,
    title: project.title || "当前作品",
    genre: project.genre || "",
    platform: project.platform || "",
    storyLengthLabel: project.storyLengthLabel || "未设置篇幅",
    targetWords: project.targetWords || 0,
    targetChapters: project.targetChapters || 0,
    chapterNumber: project.chapterNumber || 1,
    chapterTitle: project.chapterTitle || "",
    archiveCount: appState?.chapterArchive?.length || 0,
    draftScore: appState?.draftScore || 0,
    updatedAt: appState?.updatedAt || "",
    workflowLabel: appState?.workflow?.primaryLabel || "继续写作",
    active: true,
  };
}

function renderProjectShelf() {
  const list = document.getElementById("projectShelfList");
  if (!list || !appState) return;
  const projects = projectList.length ? projectList : [currentProjectSummary()];
  list.innerHTML = projects
    .map((project) => {
      const active = project.active ? " active" : "";
      const buttonText = project.active ? "当前打开" : "打开继续写";
      return `
        <article class="project-card${active}">
          <div class="project-card-main">
            <span>${project.active ? "当前作品" : "书架作品"}</span>
            <h3>${escapeHtml(project.title)}</h3>
            <p>第 ${project.chapterNumber || 1} 章 · ${escapeHtml(project.chapterTitle || "待命名章节")}</p>
          </div>
          <div class="project-card-meta">
            <span>已完成 ${project.archiveCount || 0} 章</span>
            <span>${escapeHtml(project.storyLengthLabel || "未设置篇幅")} · ${formatWords(project.targetWords)}</span>
            <span>评分 ${project.draftScore || 0}</span>
            <span>${escapeHtml(project.workflowLabel || "继续写作")}</span>
            <span>${formatDateTime(project.updatedAt)}</span>
          </div>
          <div class="project-card-actions">
            <button class="${project.active ? "secondary-button" : "primary-button"} shelf-open" type="button" data-project="${escapeHtml(
              project.id,
            )}" ${project.active ? "disabled" : ""}>${buttonText}</button>
            <button class="danger-button shelf-delete" type="button" data-project="${escapeHtml(project.id)}" data-title="${escapeHtml(project.title)}">删除</button>
          </div>
        </article>
      `;
    })
    .join("");

  list.querySelectorAll(".shelf-open").forEach((button) => {
    button.addEventListener("click", () => switchProject(button.dataset.project, button));
  });

  list.querySelectorAll(".shelf-delete").forEach((button) => {
    button.addEventListener("click", async () => {
      const title = button.dataset.title || "这本书";
      if (!window.confirm(`确认删除《${title}》?\n\n该操作不可撤销,所有章节内容会一并清空。`)) return;
      const originalText = button.textContent;
      button.disabled = true;
      button.textContent = "删除中...";
      try {
        const payload = await mutate("/api/projects/delete", { projectId: button.dataset.project }, "已删除作品");
        if (payload) {
          projectList = await API.getProjects().catch(() => projectList);
          renderProjectShelf();
          renderAll();
        }
      } finally {
        button.disabled = false;
        button.textContent = originalText;
      }
    });
  });
}

function renderBookOverview() {
  const summary = document.getElementById("bookSummary");
  const archiveList = document.getElementById("archiveList");
  const outlineList = document.getElementById("bookOutlineList");
  if (!summary || !archiveList || !outlineList) return;

  const archive = appState.chapterArchive || [];
  const current = appState.project;
  const totalWords =
    archive.reduce(
      (sum, chapter) => sum + (chapter.scenes || []).reduce((sceneSum, scene) => sceneSum + (scene.words || scene.content?.length || 0), 0),
      0,
    ) + appState.scenes.reduce((sum, scene) => sum + (scene.words || scene.content?.length || 0), 0);

  summary.innerHTML = `
    <h3>${current.title}</h3>
    <p>${current.synopsis || appState.ideaDraft?.selectedSynopsis || appState.ideaDraft?.premise || appState.memory?.canon?.[0]?.text || "当前作品正在创作中。"}</p>
    <div class="summary-grid">
      <div class="summary-tile"><span>当前章节</span><strong>${current.chapterNumber}</strong></div>
      <div class="summary-tile"><span>已归档</span><strong>${archive.length}</strong></div>
      <div class="summary-tile"><span>当前评分</span><strong>${appState.draftScore || 0}</strong></div>
      <div class="summary-tile"><span>累计字数</span><strong>${totalWords}</strong></div>
      <div class="summary-tile"><span>篇幅规划</span><strong>${current.storyLengthLabel || "未设置"}</strong></div>
      <div class="summary-tile"><span>目标规模</span><strong>${formatWords(current.targetWords)} / ${current.targetChapters || "--"}章</strong></div>
    </div>
  `;

  archiveList.innerHTML = archive.length
    ? archive
        .slice()
        .sort((a, b) => a.number - b.number)
        .map(
          (chapter) => `
            <article class="archive-card">
              <span>第 ${chapter.number} 章 · ${chapter.truthAfter || "已归档"}</span>
              <h3>${chapter.title}</h3>
              <p>${(chapter.scenes || []).length} 个场景 · 评分 ${chapter.draftScore || 0}</p>
              <div class="archive-actions">
                <button class="secondary-button archive-read" type="button" data-chapter="${chapter.number}">查看正文</button>
                <button class="secondary-button archive-export" type="button" data-chapter="${chapter.number}">导出本章</button>
              </div>
            </article>
          `,
        )
        .join("")
    : '<article class="archive-card"><h3>还没有归档章节</h3><p>写完本章并写入真相文件后，会自动出现在这里。</p></article>';

  archiveList.querySelectorAll(".archive-read").forEach((button) => {
    button.addEventListener("click", () => openChapterReader(Number(button.dataset.chapter)));
  });

  archiveList.querySelectorAll(".archive-export").forEach((button) => {
    button.addEventListener("click", () => {
      downloadExport(`/api/export/markdown?chapter=${button.dataset.chapter}`);
    });
  });

  const outlines = appState.outline?.chapterOutlines || appState.ideaDraft?.chapterOutlines || [];
  outlineList.innerHTML = outlines.length
    ? outlines
        .map((chapter) => {
          const active = Number(chapter.chapter) === Number(current.chapterNumber) ? " · 当前" : "";
          return `
            <article class="outline-mini-card">
              <span>第 ${chapter.chapter} 章${active}</span>
              <h3>${chapter.title}</h3>
              <p>${chapter.coreEvent}</p>
              <p>钩子：${chapter.readerHook}</p>
            </article>
          `;
        })
        .join("")
    : '<article class="outline-mini-card"><h3>暂无细纲</h3><p>使用“题材成书”创建作品后，这里会显示前 10 章细纲。</p></article>';
}

async function loadAndOpenProjectShelf() {
  try {
    projectList = await API.getProjects();
    renderProjectShelf();
    openProjectShelfModal();
  } catch (error) {
    showToast(`书架加载失败：${error.message}`);
  }
}

async function switchProject(projectId, button) {
  if (!projectId) return;
  if (editorDirty && !window.confirm("当前正文有未保存修改，切换作品后会丢失。继续切换吗？")) {
    return;
  }
  setButtonBusy(button, true, "正在打开...");
  try {
    const payload = await mutate("/api/projects/switch", { projectId }, "已切换作品");
    if (payload) {
      resetWorkspaceSelection(payload.state);
      projectList = await API.getProjects().catch(() => projectList);
      renderAll();
      closeProjectShelfModal();
      switchView("workspace");
    }
  } finally {
    setButtonBusy(button, false);
  }
}

function openChapterReader(chapterNumber) {
  const chapter = (appState.chapterArchive || []).find((item) => Number(item.number) === Number(chapterNumber));
  if (!chapter) {
    showToast("没有找到这个归档章节");
    return;
  }
  document.getElementById("chapterReaderTitle").textContent = `第 ${chapter.number} 章 · ${chapter.title}`;
  document.getElementById("chapterReaderMeta").textContent = `${(chapter.scenes || []).length} 个场景 · 评分 ${
    chapter.draftScore || 0
  } · ${chapter.truthAfter || "已归档"}`;
  document.getElementById("readerExportBtn").dataset.chapter = chapter.number;
  document.getElementById("chapterReaderContent").innerHTML = (chapter.scenes || [])
    .map(
      (scene) => `
        <section class="reader-scene">
          <h3>${scene.number}. ${escapeHtml(scene.title)}</h3>
          <p>${escapeHtml(scene.summary || scene.type || "")}</p>
          <div class="reader-text">${escapeHtml(scene.content || "")}</div>
        </section>
      `,
    )
    .join("");

  const modal = document.getElementById("chapterReaderModal");
  modal.classList.add("is-open");
  modal.setAttribute("aria-hidden", "false");
}

function renderWorkflow() {
  const workflow = appState.workflow;
  if (!workflow) return;
  document.getElementById("workflowNextTitle").textContent = workflow.primaryLabel;
  document.getElementById("workflowHelper").textContent = workflow.helperText;
  document.getElementById("workflowPrimaryBtn").textContent = workflow.primaryLabel;
  document.getElementById("runWorkflowBtn").textContent = workflow.primaryLabel;
  document.getElementById("workflowSteps").innerHTML = workflow.steps
    .map((step) => {
      const active = step.id === workflow.currentStep || (workflow.currentStep === "lock" && step.id === "plan");
      const cls = step.done ? "done" : active ? "active" : "";
      return `<div class="workflow-step ${cls}">${step.label}</div>`;
    })
    .join("");
  renderActionAvailability();
}

function renderActionAvailability() {
  const step = appState.workflow?.currentStep;
  const scene = currentScene();
  const anyReady = appState.scenes?.some((item) => item.status === "ready");
  const hasIssue = appState.issues?.some((issue) => ["major", "blocker"].includes(issue.level));
  const setDisabled = (id, disabled) => {
    const button = document.getElementById(id);
    if (button) button.disabled = disabled;
  };

  setDisabled("planBtn", step !== "plan");
  setDisabled("lockPlanBtn", step !== "lock");
  setDisabled("writeScenesBtn", step !== "write");
  setDisabled("auditBtn", step !== "audit" && !anyReady);
  setDisabled("reviseSceneBtn", step !== "revise" && scene?.status !== "needs_fix" && !hasIssue);
  setDisabled("styleEditBtn", step !== "style" && !anyReady);
  setDisabled("settleBtn", step !== "settle");
  setDisabled("finishNextChapterBtn", step !== "export");
  setDisabled("workflowAutoBtn", step === "export");
}

function pendingIdeaDraft() {
  return appState?.pendingIdeaDraft || null;
}

function ideaSynopsisOptions(idea) {
  return (idea?.synopsisOptions || [])
    .map((item, index) => {
      if (typeof item === "string") return { style: `简介 ${index + 1}`, text: item };
      return {
        style: item.style || item.title || `简介 ${index + 1}`,
        text: item.text || item.content || "",
      };
    })
    .filter((item) => item.text);
}

function chooseIdeaOption(type, index) {
  const idea = pendingIdeaDraft();
  if (!idea) return;
  if (type === "title") {
    idea.selectedTitle = idea.recommendedTitles[index] || idea.selectedTitle;
  }
  if (type === "hero") {
    idea.selectedProtagonist = idea.recommendedProtagonists[index] || idea.selectedProtagonist;
  }
  if (type === "synopsis") {
    const option = ideaSynopsisOptions(idea)[index];
    if (option) {
      idea.selectedSynopsis = option.text;
      idea.selectedSynopsisStyle = option.style;
    }
  }
  renderIdeaDraft();
}

function renderIdeaDraft() {
  const idea = pendingIdeaDraft();
  const panel = document.getElementById("ideaResult");
  if (!panel) return;
  if (!idea) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  document.getElementById("ideaTitle").textContent = `推荐：《${idea.selectedTitle}》`;
  document.getElementById("ideaPremise").textContent = idea.selectedSynopsis || idea.premise;
  document.getElementById("ideaLength").textContent = `${idea.storyLengthLabel || "长篇连载"} · 目标 ${formatWords(idea.targetWords)} · ${
    idea.targetChapters || "--"
  } 章；${idea.planningRule || "按当前篇幅生成大纲和章节细纲。"}`;
  renderIdeaGenrePicker(idea);
  renderCandidateSyncBanner(idea);
  setIdeaBoxText("ideaSelectedSynopsis", idea.selectedSynopsis || "");
  setIdeaBoxText("ideaSellingPoint", idea.sellingPoint);
  setIdeaBoxText("ideaWorld", idea.worldSetting);
  setIdeaBoxText("ideaMainConflict", idea.mainConflict || "");
  setIdeaBoxText("ideaFirstGoal", idea.firstGoal || "");
  const deep = idea.deepRules || {};
  document.getElementById("ideaDeepRules").innerHTML = `
    <div class="genre-rule-card">
      <strong>核心机制</strong>
      <ul><li>${deep.coreMechanism || idea.mainConflict}</li></ul>
    </div>
    <div class="genre-rule-card">
      <strong>开篇打法</strong>
      <ul>${(deep.openingRecipe || []).map((item) => `<li>${item}</li>`).join("")}</ul>
    </div>
    <div class="genre-rule-card">
      <strong>爽点引擎</strong>
      <ul>${(deep.payoffEngine || []).map((item) => `<li>${item}</li>`).join("")}</ul>
    </div>
    <div class="genre-rule-card">
      <strong>避坑禁忌</strong>
      <ul>${(deep.avoid || []).map((item) => `<li>${item}</li>`).join("")}</ul>
    </div>
  `;
  document.getElementById("ideaTitles").innerHTML = `<div class="pill-list">${idea.recommendedTitles
    .map(
      (title, index) =>
        `<button class="choice-pill selectable ${title === idea.selectedTitle ? "selected" : ""}" type="button" data-idea-choice="title" data-index="${index}">${escapeHtml(
          title,
        )}</button>`,
    )
    .join("")}</div>`;
  document.getElementById("ideaHeroes").innerHTML = `<div class="pill-list">${idea.recommendedProtagonists
    .map(
      (hero, index) =>
        `<button class="choice-pill selectable ${hero === idea.selectedProtagonist ? "selected" : ""}" type="button" data-idea-choice="hero" data-index="${index}">${escapeHtml(
          hero,
        )}</button>`,
    )
    .join("")}</div>`;
  document.getElementById("ideaSynopsisOptions").innerHTML = ideaSynopsisOptions(idea)
    .map(
      (option, index) => `
        <button class="synopsis-choice ${option.text === idea.selectedSynopsis ? "selected" : ""}" type="button" data-idea-choice="synopsis" data-index="${index}">
          <strong>${escapeHtml(option.style)}</strong>
          <span>${escapeHtml(option.text)}</span>
        </button>
      `,
    )
    .join("");
  document.querySelectorAll("[data-idea-choice]").forEach((button) => {
    button.addEventListener("click", () => chooseIdeaOption(button.dataset.ideaChoice, Number(button.dataset.index)));
  });
  document.getElementById("ideaArcs").innerHTML = idea.arcs
    .map(
      (arc) => `
        <article class="outline-card">
          <strong>${arc.name} · ${arc.chapters}</strong>
          <p>${arc.goal}</p>
          <p>阶段兑现：${arc.payoff}</p>
        </article>
      `,
    )
    .join("");
  document.getElementById("ideaChapters").innerHTML = idea.chapterOutlines
    .map(
      (chapter) => `
        <article class="chapter-outline-card">
          <strong>${chapter.chapter}. ${chapter.title}</strong>
          <p>${chapter.coreEvent}</p>
          <p>钩子：${chapter.readerHook}</p>
        </article>
      `,
    )
    .join("");
  renderAllBoxActions(idea);
  renderIdeaConfirm(idea);
}

function setIdeaBoxText(elementId, value) {
  const el = document.getElementById(elementId);
  if (el) el.textContent = value || "（待补充）";
}

// 流派枚举(键名必须与后端 GENRE_DEEP_CONFIGS 对齐)
const GENRE_OPTIONS = [
  { key: "xuanhuan", label: "玄幻修仙" },
  { key: "urban", label: "都市系统" },
  { key: "rebirth", label: "重生/穿越" },
  { key: "romance", label: "情感言情" },
  { key: "scifi", label: "科幻/星际" },
  { key: "weird_rules", label: "规则怪谈/诡异复苏" },
  { key: "beast_taming", label: "御兽" },
  { key: "infinite", label: "无限流/副本" },
  { key: "gaowu", label: "高武/灵气复苏" },
  { key: "apocalypse", label: "末世囤货/天灾" },
  { key: "game_invasion", label: "游戏入侵/第四天灾" },
  { key: "live_entertainment", label: "直播文娱" },
  { key: "farming_building", label: "种田基建/经营" },
  { key: "mystery_horror", label: "克苏鲁/民俗悬疑" },
];

function renderIdeaGenrePicker(idea) {
  const labelEl = document.getElementById("ideaGenreLabel");
  const select = document.getElementById("ideaGenreSelect");
  if (!labelEl || !select) return;
  const currentKey = idea.genre || "xuanhuan";
  const currentLabel = idea.genreLabel || GENRE_OPTIONS.find((g) => g.key === currentKey)?.label || currentKey;
  labelEl.textContent = currentLabel;
  // 第一次渲染时填 options
  if (select.dataset.bound !== "1") {
    select.innerHTML =
      `<option value="">↻ 不对，改成...</option>` +
      GENRE_OPTIONS.map((g) => `<option value="${g.key}">${escapeHtml(g.label)}</option>`).join("");
    select.addEventListener("change", async (event) => {
      const target = event.target.value;
      if (!target) return;
      const ideaNow = pendingIdeaDraft();
      if (!ideaNow) return;
      if (target === ideaNow.genre) {
        event.target.value = "";
        return;
      }
      const oldValue = event.target.value;
      event.target.disabled = true;
      try {
        await mutate("/api/ideation/set-genre", { genre: target }, "已改判流派");
      } finally {
        event.target.disabled = false;
        // 重新渲染会重置 value
      }
    });
    select.dataset.bound = "1";
  }
  // 每次渲染都重置 select 到占位符
  select.value = "";
}

// 改判流派后,渲染「同步候选」横幅(标题/主角/简介候选还是旧流派 LLM 生成的)
function renderCandidateSyncBanner(idea) {
  const host = document.getElementById("ideaGenreBox");
  if (!host) return;
  let banner = host.querySelector(".candidate-sync-banner");
  const stale = idea && idea.candidatesStaleAfterGenre;
  if (!stale) {
    if (banner) banner.remove();
    return;
  }
  // 本次会话内用户已经点了「× 不用了」(同一 stale 来源才抑制,改判后再次出现的横幅会重新弹)
  if (host.dataset.syncDismissedFor === stale) {
    if (banner) banner.remove();
    return;
  }
  // 来源变了或者没 dismiss 过,清掉旧的 dismiss 标记
  if (host.dataset.syncDismissedFor && host.dataset.syncDismissedFor !== stale) {
    delete host.dataset.syncDismissedFor;
  }
  const currentLabel = idea.genreLabel || idea.genre || "当前流派";
  if (!banner) {
    banner = document.createElement("div");
    banner.className = "candidate-sync-banner";
    host.appendChild(banner);
  }
  banner.innerHTML = `
    <span class="sync-text">「标题 / 主角 / 简介」候选还是按「${escapeHtml(stale)}」生成的,要不要同步成「${escapeHtml(currentLabel)}」?</span>
    <button type="button" class="box-action-btn sync-btn">✨ 一键同步候选</button>
    <button type="button" class="sync-dismiss" title="本次不用">×</button>
  `;
  const syncBtn = banner.querySelector(".sync-btn");
  const dismissBtn = banner.querySelector(".sync-dismiss");
  syncBtn.addEventListener("click", async () => {
    syncBtn.disabled = true;
    syncBtn.textContent = "同步中...";
    try {
      await mutate("/api/ideation/regenerate-candidates", {}, "候选已同步");
    } finally {
      // 失败时 mutate 已经 toast,横幅由后端 state 决定是否还在
      syncBtn.disabled = false;
    }
  });
  dismissBtn.addEventListener("click", () => {
    host.dataset.syncDismissedFor = stale;
    banner.remove();
  });
}

// 单文本字段(可手改 + AI 打磨)
const POLISH_TEXT_FIELDS = new Set([
  "selectedSynopsis",
  "sellingPoint",
  "worldSetting",
  "mainConflict",
  "firstGoal",
]);
// 数组字段(只支持整组重生成)
const POLISH_ARRAY_FIELDS = new Set([
  "recommendedTitles",
  "recommendedProtagonists",
  "synopsisOptions",
]);
const POLISH_FIELD_LABELS = {
  selectedSynopsis: "简介",
  sellingPoint: "核心卖点",
  worldSetting: "世界观",
  mainConflict: "主冲突",
  firstGoal: "第一章目标",
  recommendedTitles: "推荐书名",
  recommendedProtagonists: "主角名候选",
  synopsisOptions: "简介候选",
};

function renderAllBoxActions(idea) {
  document.querySelectorAll(".idea-box[data-field]").forEach((box) => {
    const field = box.dataset.field;
    const actions = box.querySelector(".box-actions");
    if (!actions) return;
    renderBoxActions(actions, field, idea);
  });
}

function renderBoxActions(container, field, idea) {
  const revisions = (idea.revisions || []).filter((rev) => rev.field === field);
  const isText = POLISH_TEXT_FIELDS.has(field);
  const isArray = POLISH_ARRAY_FIELDS.has(field);
  const buttons = [];
  if (isText) {
    buttons.push(
      `<button type="button" class="box-action-btn" data-polish="edit" data-field="${field}" title="手动编辑">✏️ 我来改</button>`,
    );
  }
  if (isText || isArray) {
    buttons.push(
      `<button type="button" class="box-action-btn" data-polish="refine" data-field="${field}" title="AI 按指令改写">✨ AI 改写</button>`,
    );
  }
  buttons.push(
    `<button type="button" class="box-action-btn" data-polish="history" data-field="${field}" title="查看修订历史">📜 历史 (${revisions.length})</button>`,
  );
  container.innerHTML = buttons.join("");
  container.querySelectorAll(".box-action-btn").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const action = btn.dataset.polish;
      if (action === "edit") openInlineEditor(field);
      else if (action === "refine") openRefineDialog(field);
      else if (action === "history") openHistoryDrawer(field);
    });
  });
  // 点正文区域 = 进入手改(只对单文本字段生效)
  if (isText) {
    const box = container.closest(".idea-box");
    if (box) {
      const target = box.querySelector(":scope > p");
      if (target && !target.dataset.clickEditBound) {
        target.dataset.clickEditBound = "1";
        target.addEventListener("click", (ev) => {
          if (box.querySelector(".inline-editor")) return;
          ev.stopPropagation();
          openInlineEditor(field);
        });
      }
    }
  }
}

function findIdeaBoxByField(field) {
  return document.querySelector(`.idea-box[data-field="${field}"]`);
}

function openInlineEditor(field) {
  const idea = pendingIdeaDraft();
  if (!idea) return;
  if (!POLISH_TEXT_FIELDS.has(field)) {
    showToast("该字段不支持手改");
    return;
  }
  const box = findIdeaBoxByField(field);
  if (!box) return;
  // 防止重复打开
  if (box.querySelector(".inline-editor")) return;
  const current = idea[field] || "";
  const label = POLISH_FIELD_LABELS[field] || field;
  const editor = document.createElement("div");
  editor.className = "inline-editor";
  editor.innerHTML = `
    <textarea class="inline-editor-textarea" rows="5" placeholder="编辑${escapeHtml(label)}...">${escapeHtml(current)}</textarea>
    <div class="inline-editor-actions">
      <button type="button" class="secondary-button inline-cancel">取消</button>
      <button type="button" class="primary-button inline-save">保存</button>
    </div>
  `;
  box.appendChild(editor);
  const textarea = editor.querySelector("textarea");
  textarea.focus();
  editor.querySelector(".inline-cancel").addEventListener("click", () => editor.remove());
  editor.querySelector(".inline-save").addEventListener("click", async () => {
    const value = textarea.value.trim();
    if (!value) {
      showToast("内容不能为空");
      return;
    }
    if (value === current.trim()) {
      editor.remove();
      return;
    }
    const saveBtn = editor.querySelector(".inline-save");
    setButtonBusy(saveBtn, true, "保存中...");
    try {
      await mutate("/api/ideation/patch", { field, value }, "已记录手改");
    } finally {
      setButtonBusy(saveBtn, false);
    }
  });
}

function openRefineDialog(field) {
  const idea = pendingIdeaDraft();
  if (!idea) return;
  if (!POLISH_TEXT_FIELDS.has(field) && !POLISH_ARRAY_FIELDS.has(field)) {
    showToast("该字段不支持 AI 打磨");
    return;
  }
  const label = POLISH_FIELD_LABELS[field] || field;
  const isArray = POLISH_ARRAY_FIELDS.has(field);
  const hint = isArray
    ? `将由 AI 重生成「${label}」整组候选。可以补充期望方向，例如「更黑暗一点」「更偏都市感」。`
    : `输入打磨指令，AI 将基于现有「${label}」内容改写。例如「更克制」「加一句钩子」。`;
  const existing = document.getElementById("refineDialog");
  if (existing) existing.remove();
  const backdrop = document.createElement("div");
  backdrop.id = "refineDialog";
  backdrop.className = "modal-backdrop refine-dialog";
  backdrop.innerHTML = `
    <section class="modal refine-modal" role="dialog" aria-modal="true">
      <header class="modal-header">
        <h3>✨ AI 打磨：${escapeHtml(label)}</h3>
        <button type="button" class="icon-button refine-close" aria-label="关闭">✕</button>
      </header>
      <div class="modal-body">
        <p class="refine-hint">${escapeHtml(hint)}</p>
        <textarea class="refine-instruction" rows="4" placeholder="例如：${escapeHtml(isArray ? "更带玄幻打怪升级感" : "更克制，多一点画面感")}"></textarea>
      </div>
      <div class="modal-actions">
        <button type="button" class="secondary-button refine-close">取消</button>
        <button type="button" class="primary-button refine-submit">${isArray ? "重生成整组" : "AI 打磨"}</button>
      </div>
    </section>
  `;
  document.body.appendChild(backdrop);
  backdrop.classList.add("show");
  backdrop.setAttribute("aria-hidden", "false");
  const close = () => {
    backdrop.remove();
  };
  backdrop.querySelectorAll(".refine-close").forEach((btn) => btn.addEventListener("click", close));
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) close();
  });
  const textarea = backdrop.querySelector(".refine-instruction");
  textarea.focus();
  const submit = backdrop.querySelector(".refine-submit");
  submit.addEventListener("click", async () => {
    const instruction = textarea.value.trim();
    if (!instruction) {
      showToast("请填写打磨指令");
      return;
    }
    setButtonBusy(submit, true, "AI 改写中...");
    try {
      const payload = await mutate("/api/ideation/refine", { field, instruction }, "AI 打磨完成");
      if (payload) {
        close();
      }
    } finally {
      setButtonBusy(submit, false);
    }
  });
}

function openHistoryDrawer(field) {
  const idea = pendingIdeaDraft();
  if (!idea) return;
  const label = POLISH_FIELD_LABELS[field] || field;
  const revisions = (idea.revisions || []).filter((rev) => rev.field === field);
  const existing = document.getElementById("historyDrawer");
  if (existing) existing.remove();
  const backdrop = document.createElement("div");
  backdrop.id = "historyDrawer";
  backdrop.className = "modal-backdrop history-drawer";
  const body = revisions.length
    ? revisions
        .slice()
        .reverse()
        .map((rev) => {
          const sourceTag =
            rev.source === "ai"
              ? '<span class="rev-tag ai">✨ AI</span>'
              : rev.source === "manual"
                ? '<span class="rev-tag manual">✏️ 手改</span>'
                : '<span class="rev-tag revert">↩ 回退</span>';
          const instr = rev.instruction ? `<p class="rev-instruction">指令：${escapeHtml(rev.instruction)}</p>` : "";
          return `
            <article class="rev-card" data-rev-id="${escapeHtml(rev.id)}">
              <header>
                ${sourceTag}
                <span class="rev-ts">${escapeHtml(formatDateTime(rev.ts))}</span>
                <button type="button" class="secondary-button rev-revert" data-rev-id="${escapeHtml(rev.id)}">↩ 恢复此版本</button>
              </header>
              ${instr}
              ${renderDiffPair(rev.before, rev.after)}
            </article>
          `;
        })
        .join("")
    : `<p class="rev-empty">还没有修订历史。点 ✏️ 改 或 ✨ AI 打磨 来开始这个字段的迭代。</p>`;
  backdrop.innerHTML = `
    <section class="modal history-modal" role="dialog" aria-modal="true">
      <header class="modal-header">
        <h3>📜 修订历史：${escapeHtml(label)} (${revisions.length})</h3>
        <button type="button" class="icon-button history-close" aria-label="关闭">✕</button>
      </header>
      <div class="modal-body history-body">
        ${body}
      </div>
      <div class="modal-actions">
        <button type="button" class="secondary-button history-close">关闭</button>
      </div>
    </section>
  `;
  document.body.appendChild(backdrop);
  backdrop.classList.add("show");
  backdrop.setAttribute("aria-hidden", "false");
  const close = () => backdrop.remove();
  backdrop.querySelectorAll(".history-close").forEach((btn) => btn.addEventListener("click", close));
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) close();
  });
  backdrop.querySelectorAll(".rev-revert").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const revisionId = btn.dataset.revId;
      setButtonBusy(btn, true, "回退中...");
      try {
        const payload = await mutate("/api/ideation/revert", { revisionId }, "已回退");
        if (payload) close();
      } finally {
        setButtonBusy(btn, false);
      }
    });
  });
}

function renderDiffPair(before, after) {
  const fmt = (value) => {
    if (Array.isArray(value)) {
      return value
        .map((item) => {
          if (item && typeof item === "object") {
            return `<li>${escapeHtml(item.style || "")} ｜ ${escapeHtml(item.text || item.content || "")}</li>`;
          }
          return `<li>${escapeHtml(String(item))}</li>`;
        })
        .join("");
    }
    return escapeHtml(String(value ?? ""));
  };
  const isGenreSnapshot = (v) => v && typeof v === "object" && !Array.isArray(v) && ("genreLabel" in v || "genre" in v);
  const wrap = (value) => {
    if (isGenreSnapshot(value)) {
      return `<div class="diff-text"><strong>${escapeHtml(value.genreLabel || value.genre || "(未知)")}</strong></div>`;
    }
    if (Array.isArray(value)) {
      return `<ul class="diff-list">${fmt(value)}</ul>`;
    }
    return `<div class="diff-text">${fmt(value)}</div>`;
  };
  return `
    <div class="diff-pair">
      <div class="diff-cell diff-before">
        <strong>修订前</strong>
        ${wrap(before)}
      </div>
      <div class="diff-arrow">→</div>
      <div class="diff-cell diff-after">
        <strong>修订后</strong>
        ${wrap(after)}
      </div>
    </div>
  `;
}

function renderIdeaConfirm(idea) {
  const checkbox = document.getElementById("ideaConfirmCheckbox");
  const adoptBtn = document.getElementById("adoptIdeaBtn");
  if (checkbox) {
    checkbox.checked = !!idea.confirmed;
  }
  if (adoptBtn) {
    if (idea.confirmed) {
      adoptBtn.disabled = false;
      adoptBtn.textContent = "采用方案创建作品";
    } else {
      adoptBtn.disabled = true;
      adoptBtn.textContent = "先勾选「确认设定」再创建";
    }
  }
}

function renderScenes() {
  const board = document.getElementById("sceneBoard");
  board.innerHTML = appState.scenes
    .map((scene) => {
      const isActive = scene.id === currentScene()?.id ? " active" : "";
      const statusTag =
        scene.status === "needs_fix"
          ? '<span class="tag warning">待修</span>'
          : scene.status === "ready"
            ? '<span class="tag success">已生成</span>'
            : '<span class="tag">草稿</span>';
      const tags = [scene.type, ...normalizeTags(scene.tags)]
        .slice(0, 3)
        .map((tag) => `<span class="tag">${tag}</span>`)
        .join("");
      return `
        <button class="scene-card${isActive}" type="button" data-scene="${scene.id}">
          <span class="scene-number">${scene.number}</span>
          <h3>${scene.title}</h3>
          <p>${scene.summary}</p>
          <div class="tag-row">${tags}${statusTag}</div>
        </button>
      `;
    })
    .join("");

  board.querySelectorAll(".scene-card").forEach((button) => {
    button.addEventListener("click", () => {
      if (editorDirty && !window.confirm("当前场景有未保存修改，切换后会丢失。继续切换吗？")) {
        return;
      }
      selectedSceneId = button.dataset.scene;
      renderScenes();
      renderEditor();
      showToast(`已选择 ${currentScene().title}`);
    });
  });
}

function renderEditor() {
  const scene = currentScene();
  if (!scene) return;
  selectedSceneId = scene.id;
  document.getElementById("editorMeta").textContent = `${scene.id} · ${scene.type} · ${scene.words} 字`;
  document.getElementById("chapterEditor").textContent = scene.content;
  editorDirty = false;
  updateSaveButton();
}

function renderPatches() {
  document.getElementById("patchList").innerHTML = appState.patches
    .map(
      (patch) => `
        <div class="patch-item">
          <span>${patch.type}</span>
          <strong>${patch.title}</strong>
          <p>${patch.detail}</p>
        </div>
      `,
    )
    .join("");
}

function renderMemory() {
  const items = (appState.memory || {})[selectedMemory] || [];
  document.getElementById("memoryGrid").innerHTML = items
    .map(
      (item) => `
        <article class="memory-card">
          <h3>${item.title}</h3>
          <p>${item.text}</p>
        </article>
      `,
    )
    .join("");
}

function renderHooks() {
  const hookColumns = [
    ["planned", "planned"],
    ["planted", "planted"],
    ["reinforced", "reinforced"],
    ["near_due", "near_due"],
    ["resolved", "resolved"],
  ];
  document.getElementById("hookBoard").innerHTML = hookColumns
    .map(([status, title]) => {
      const cards = appState.hooks
        .filter((hook) => hook.status === status)
        .map(
          (hook) => `
            <article class="hook-card">
              <h3>${hook.title}</h3>
              <p>${hook.text}</p>
              <div class="meta"><span>${hook.meta}</span></div>
            </article>
          `,
        )
        .join("");
      return `
        <section class="kanban-column">
          <h3>${title}</h3>
          ${cards || '<article class="hook-card"><p>暂无伏笔</p></article>'}
        </section>
      `;
    })
    .join("");
}

function relationshipProfile(key, character, protagonist) {
  const profiles = {
    suHan: { cls: "protagonist", label: "主视角", relation: "叙事中心", status: "所有人物线都围绕主角当前选择推进。" },
    chenXuan: { cls: "antagonist", label: "公开冲突", relation: "压迫 / 对抗", status: "制造直接阻力，负责阶段打脸和冲突升级。" },
    elder: { cls: "mentor", label: "隐藏观察", relation: "伏笔 / 观察", status: "提供高层信息差，但不能直接替主角解决问题。" },
    suXuewen: { cls: "ally", label: "旧关系线", relation: "情感 / 旧因", status: "承担关系反转、旧事压力或未来盟友功能。" },
    outerSect: { cls: "faction", label: "规则压力", relation: "制度 / 环境", status: "代表规则和平台压力，限制主角行动空间。" },
  };
  if (profiles[key]) return profiles[key];
  if (character.role?.includes("反派")) return profiles.chenXuan;
  if (character.role?.includes("隐藏") || character.role?.includes("观察")) return profiles.elder;
  if (character.role?.includes("规则") || character.role?.includes("势力")) return profiles.outerSect;
  return { cls: "ally", label: "关联人物", relation: `${protagonist.name} 相关线`, status: character.motive || "辅助推进当前单元。" };
}

function renderCharacter() {
  const entries = Object.entries(appState.characters || {});
  if (!entries.length) return;
  const protagonistEntry = entries.find(([key]) => key === "suHan") || entries[0];
  const protagonist = protagonistEntry[1];
  const selectedEntry = entries.find(([key]) => key === selectedCharacter) || protagonistEntry;
  selectedCharacter = selectedEntry[0];
  const character = selectedEntry[1];
  const positions = [
    { left: 50, top: 47 },
    { left: 18, top: 18 },
    { left: 80, top: 20 },
    { left: 20, top: 75 },
    { left: 78, top: 74 },
    { left: 50, top: 15 },
    { left: 50, top: 82 },
  ];
  const center = positions[0];
  const nodes = entries.map(([key, item], index) => {
    const profile = relationshipProfile(key, item, protagonist);
    const position = key === protagonistEntry[0] ? center : positions[index] || positions[(index % (positions.length - 1)) + 1];
    return { key, item, profile, position };
  });
  const lines = nodes
    .filter((node) => node.key !== protagonistEntry[0])
    .map(
      (node) =>
        `<line x1="${center.left * 7.2}" y1="${center.top * 4.2}" x2="${node.position.left * 7.2}" y2="${node.position.top * 4.2}"></line>`,
    )
    .join("");
  document.getElementById("relationMap").innerHTML = `
    <svg class="relation-lines" viewBox="0 0 720 420" aria-hidden="true">${lines}</svg>
    ${nodes
      .map(
        (node) => `
          <button class="person-node ${node.profile.cls} ${node.key === selectedCharacter ? "active" : ""}" style="left:${node.position.left}%; top:${node.position.top}%" type="button" data-character="${node.key}">
            <strong>${escapeHtml(node.item.name)}</strong>
            <span>${escapeHtml(node.profile.label)}</span>
          </button>
        `,
      )
      .join("")}
  `;
  document.getElementById("characterDetail").innerHTML = `
    <h2>${escapeHtml(character.name)}</h2>
    <p>${escapeHtml(character.role)}</p>
    <div class="detail-list">
      <div><span>当前状态</span><strong>${escapeHtml(character.state)}</strong></div>
      <div><span>动机</span><strong>${escapeHtml(character.motive)}</strong></div>
      <div><span>能力边界</span><strong>${escapeHtml(character.ability)}</strong></div>
      <div><span>不可违反</span><strong>${escapeHtml(character.constraints)}</strong></div>
    </div>
  `;
  document.getElementById("relationshipList").innerHTML = nodes
    .filter((node) => node.key !== protagonistEntry[0])
    .map(
      (node) => `
        <button class="relation-card ${node.key === selectedCharacter ? "active" : ""}" type="button" data-character="${node.key}">
          <span>${escapeHtml(node.profile.relation)}</span>
          <strong>${escapeHtml(protagonist.name)} ⇄ ${escapeHtml(node.item.name)}</strong>
          <p>${escapeHtml(node.profile.status)}</p>
        </button>
      `,
    )
    .join("");
  document.querySelectorAll(".person-node, .relation-card").forEach((node) => {
    node.addEventListener("click", () => {
      selectedCharacter = node.dataset.character;
      renderCharacter();
    });
  });
}

function renderScores() {
  document.getElementById("scoreMatrix").innerHTML = appState.scores
    .map((scoreItem) => {
      const cls = scoreItem.score < 70 ? "danger" : scoreItem.score < 80 ? "warn" : "";
      return `
        <article class="score-card">
          <div class="score-head">
            <h3>${scoreItem.name}</h3>
            <span class="score-value">${scoreItem.score}</span>
          </div>
          <div class="meter"><span class="${cls}" style="width:${scoreItem.score}%"></span></div>
          <p>${scoreItem.reason}</p>
        </article>
      `;
    })
    .join("");

  const issueHtml = appState.issues.length
    ? appState.issues
        .map(
          (issue) => `
            <div class="issue-row ${issue.level === "blocker" ? "warning" : ""}">
              <strong>${issue.level} · ${issue.location}</strong>
              <span>${issue.issue}</span>
              <span>修订建议：${issue.fix}</span>
            </div>
          `,
        )
        .join("")
    : '<div class="issue-row"><strong>已通过</strong><span>当前没有阻塞问题。</span></div>';
  document.getElementById("auditIssues").innerHTML = issueHtml;

  const railIssues = document.querySelector(".right-rail .issue-list.mini");
  if (railIssues) {
    railIssues.innerHTML = issueHtml;
  }
}

function renderStyleReport() {
  const panel = document.getElementById("styleReport");
  if (!panel) return;
  const report = appState.humanStyleReport || {};
  if (report.status !== "done") {
    panel.innerHTML = `
      <div class="style-score pending">待审校</div>
      <p>正文通过审计后，系统会在写入真相文件前进行原创表达审校。</p>
    `;
    return;
  }
  const redFlags = Object.entries(report.redFlags || {})
    .slice(0, 5)
    .map(([word, count]) => `<span>${escapeHtml(word)} × ${count}</span>`)
    .join("");
  const platformGuide = report.platformGuide || {};
  panel.innerHTML = `
    <div class="style-score">${report.score || "--"}</div>
    <p>平均段长：${report.averageParagraphLength || "--"} 字</p>
    ${
      platformGuide.focus
        ? `<div class="style-platform"><strong>${escapeHtml(platformGuide.label || "平台侧重")}</strong><span>${escapeHtml(platformGuide.focus)}</span></div>`
        : ""
    }
    <div class="style-tags">${redFlags || "<span>未发现高频模板词</span>"}</div>
    <ul>${(report.issues || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
  `;
}

function renderTrace() {
  document.getElementById("runTrace").innerHTML = appState.trace
    .map(
      (item) => `
        <article class="trace-item">
          <div class="trace-time">${item.time}</div>
          <div>
            <h3>${item.title}</h3>
            <p>${item.text}</p>
          </div>
        </article>
      `,
    )
    .join("");
  document.getElementById("truthAfter").textContent = appState.truthAfter;
}

function renderLLMConfig() {
  const status = document.getElementById("llmConfigStatus");
  const list = document.getElementById("agentModelList");
  const testSelect = document.getElementById("testAgentRole");
  if (!status || !list || !testSelect) return;
  const config = llmConfig || llmStatus || {};
  status.innerHTML = `
    <strong>${config.configured ? "当前模型已可用" : "当前未配置 API Key"}</strong>
    <span>默认地址：${escapeHtml(config.baseUrl || "未设置")} · 默认模型：${escapeHtml(config.model || "未设置")}</span>
    <span>说明：网页保存的 API Key 只在当前后端进程内生效，重启后需重新填写。</span>
  `;
  const agents = config.agents || [];
  testSelect.innerHTML = ['<option value="">默认模型</option>']
    .concat(agents.map((agent) => `<option value="${agent.id}">${escapeHtml(agent.label)} · ${escapeHtml(agent.model || "默认")}</option>`))
    .join("");
  list.innerHTML = agents
    .map(
      (agent) => `
        <article class="agent-model-row" data-agent="${agent.id}">
          <div>
            <strong>${escapeHtml(agent.label)}</strong>
            <span>${escapeHtml(agent.model || "继承默认模型")} · ${agent.hasCustomApiKey ? "独立 Key" : "继承默认 Key"}</span>
          </div>
          <label>
            <span>调用地址</span>
            <input name="${agent.id}_baseUrl" type="url" placeholder="${escapeHtml(agent.baseUrl || "继承默认地址")}" />
          </label>
          <label>
            <span>模型</span>
            <input name="${agent.id}_model" type="text" placeholder="${escapeHtml(agent.customModel || agent.model || "继承默认模型")}" />
          </label>
          <label>
            <span>API Key</span>
            <input name="${agent.id}_apiKey" type="password" autocomplete="off" placeholder="${agent.hasCustomApiKey ? "已设置，留空不变" : "留空继承默认 Key"}" />
          </label>
        </article>
      `,
    )
    .join("");
}

function renderAgentSteps() {
  const steps = document.querySelectorAll(".agent-step");
  const hasBlocker = appState.issues.some((issue) => issue.level === "blocker");
  const settled = appState.truthAfter.includes("committed");
  if (steps[2]) {
    steps[2].className = `agent-step ${hasBlocker ? "active" : "done"}`;
    steps[2].querySelector("strong").textContent = hasBlocker ? "scene_03 等待修订" : "场景已通过";
  }
  if (steps[3]) {
    steps[3].className = `agent-step ${settled ? "done" : "pending"}`;
    steps[3].querySelector("strong").textContent = settled ? "TruthPatch 已写入" : "待确认 TruthPatch";
  }
}

const AGENT_TL_ORDER = ["ideation", "planner", "scene_writer", "audit", "chief_editor", "truth"];
const AGENT_TL_LABELS = {
  ideation: "🧠 题材",
  planner: "📐 章节规划",
  scene_writer: "✍️ 写手",
  audit: "🔍 审计",
  chief_editor: "📝 总编",
  truth: "📚 真相",
};
const AGENT_TL_STATUS_TEXT = {
  idle: "等待中",
  running: "运行中…",
  done: "✓ 完成",
  failed: "✗ 失败",
  revising: "重写中…",
  review_required: "⚠ 待人工",
};

function escapeAttr(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\n/g, " ");
}

function renderAgentTimeline() {
  const root = document.getElementById("agentTimeline");
  if (!root) return;
  const timeline = appState.agentTimeline || [];
  if (!timeline.length) {
    root.hidden = true;
    root.innerHTML = "";
    return;
  }
  root.hidden = false;
  // 每个 agent 取最近一条记录定状态
  const latest = {};
  for (const entry of timeline) latest[entry.agent] = entry;

  const nodes = AGENT_TL_ORDER.map((id) => {
    const entry = latest[id];
    const status = entry ? entry.status : "idle";
    const tip = entry ? `${entry.message || ""} · ${entry.ts || ""}` : "尚未开始";
    const label = AGENT_TL_LABELS[id] || id;
    const statusText = AGENT_TL_STATUS_TEXT[status] || status;
    return `<div class="agent-tl-node status-${status}" title="${escapeAttr(tip)}">
      <div class="agent-tl-emoji">${label}</div>
      <div class="agent-tl-status">${statusText}</div>
    </div>`;
  });
  root.innerHTML = `<div class="agent-tl-header">
    <span class="agent-tl-title">🎬 AI 流水线</span>
    <span class="agent-tl-hint">六个 agent 协作产出本章 · 鼠标悬停看上次输出</span>
  </div>
  <div class="agent-timeline-track">${nodes.join('<div class="agent-tl-arrow">→</div>')}</div>`;
}

function renderEditorReviewPanel() {
  const root = document.getElementById("editorReviewPanel");
  if (!root) return;
  const review = appState.editorReview;
  const requiresUser = appState.chiefEditorRequiresUser;
  if (!review && !requiresUser) {
    root.hidden = true;
    root.innerHTML = "";
    return;
  }
  if (!review) {
    root.hidden = true;
    return;
  }
  root.hidden = false;
  const score = review.score != null ? review.score : "—";
  const action = review.action || "revise";
  const issues = (review.issues || []).map((it) => {
    const sev = it.severity || "minor";
    return `<li class="editor-issue editor-issue-${sev}">
      <span class="editor-issue-sev">${sev}</span>
      <span class="editor-issue-cat">${escapeAttr(it.category || "")}</span>
      <div class="editor-issue-text">${escapeAttr(it.text || "")}</div>
      ${it.suggestion ? `<div class="editor-issue-fix">建议：${escapeAttr(it.suggestion)}</div>` : ""}
    </li>`;
  }).join("");
  const passed = action === "approve";
  const actionBadge = passed
    ? `<span class="editor-badge editor-badge-pass">${score}/100 · 通过</span>`
    : `<span class="editor-badge editor-badge-fail">${score}/100 · ${action === "reject" ? "驳回" : "打回"}</span>`;
  // 去 AI 味度量卡片 — 优先用最新一次诊断, 否则取总编审核时的快照
  const metrics = appState.lastDeAiMetrics || review.deAiMetrics || {};
  let metricsHtml = "";
  if (metrics && metrics.chars_no_punct != null) {
    const rows = [
      { label: "章末钩子", value: metrics.has_chapter_hook ? "✓ 有" : "✗ 无", ok: !!metrics.has_chapter_hook, hint: "末段需含 ?! 或'竟然/突然'等转折词" },
      { label: "单句成段率", value: `${(metrics.single_sent_para_ratio * 100).toFixed(0)}%`, ok: metrics.single_sent_para_ratio <= 0.70, hint: "爆款 P75 = 69%, 越低段落越饱满" },
      { label: "对话段比例", value: `${(metrics.dialog_para_ratio * 100).toFixed(0)}%`, ok: metrics.dialog_para_ratio >= 0.25, hint: "爆款 P25 = 27% / P50 = 37%" },
      { label: "平均句长", value: `${metrics.sent_avg} 字`, ok: metrics.sent_avg >= 14 && metrics.sent_avg <= 35, hint: "爆款 P50 = 18.4 字" },
      { label: "AI 雷区词", value: `${metrics.ai_blacklist_total} 次`, ok: metrics.ai_blacklist_total < 3, hint: Object.keys(metrics.ai_blacklist_hits || {}).slice(0, 4).join(", ") || "无" },
      { label: "升华句式", value: `${metrics.sublimation_hits} 次`, ok: metrics.sublimation_hits < 2, hint: "并非/不仅/宛若 模板" },
    ];
    metricsHtml = `<div class="editor-metrics">
      <div class="editor-metrics-title">📊 去 AI 味度量(对标七猫 32 章爆款)</div>
      <div class="editor-metrics-grid">${rows.map(r => `
        <div class="editor-metric ${r.ok ? 'ok' : 'bad'}" title="${escapeAttr(r.hint)}">
          <div class="editor-metric-label">${r.label}</div>
          <div class="editor-metric-value">${r.value}</div>
        </div>
      `).join("")}</div>
    </div>`;
  }
  const diagnoseBtn = `<button class="secondary-button" type="button" onclick="reRunDiagnostics()">🔬 重新诊断本章</button>`;
  const rewriteBtn = `<button class="primary-button editor-rewrite-btn" type="button" onclick="rewriteByEditor()">✍️ 按总编批注重写本章 (跳回正文写手)</button>`;
  const buttons = requiresUser
    ? `<div class="editor-actions">
        ${rewriteBtn}
        <button class="secondary-button" type="button" onclick="overrideEditor()">人工放行,跳过总编</button>
        ${diagnoseBtn}
      </div>`
    : (passed
        ? `<div class="editor-actions">${diagnoseBtn}</div>`
        : `<div class="editor-actions">
            ${rewriteBtn}
            <button class="secondary-button" type="button" onclick="reRunEditor()">再审一次</button>
            ${diagnoseBtn}
          </div>`);
  root.innerHTML = `<div class="editor-review-header">
    <span class="editor-title">📝 小说总编审核</span>
    ${actionBadge}
    ${review.model ? `<span class="editor-model">${escapeAttr(review.model)}</span>` : ""}
  </div>
  ${renderEditorScoreTrend()}
  ${renderAutoFixLog()}
  ${review.editorNotes ? `<div class="editor-notes">总编批注：${escapeAttr(review.editorNotes)}</div>` : ""}
  ${metricsHtml}
  ${issues ? `<ul class="editor-issues">${issues}</ul>` : ""}
  ${buttons}`;
}

function renderAutoFixLog() {
  const log = (appState && appState.autoFixLog) || [];
  if (!log.length) return "";
  // 聚合最近一轮 (按时间倒序取直到遇到上一章) — 简化:展示最近 8 条
  const recent = log.slice(-8);
  const items = recent.map((entry) => {
    const t = entry.type;
    const d = entry.detail || {};
    let icon = "🔧";
    let label = t;
    let body = "";
    if (t === "hard_rules") {
      icon = "🧹";
      label = "代码硬规则修复";
      const parts = [];
      if (d.blacklist) parts.push(`雷区词 ×${d.blacklist}`);
      if (d.sublimation) parts.push(`升华句 ×${d.sublimation}`);
      if (d.short_para_merged) parts.push(`短段合并 ×${d.short_para_merged}`);
      body = parts.join(" / ");
      if (d.blacklist_top && Object.keys(d.blacklist_top).length) {
        const top = Object.entries(d.blacklist_top).slice(0, 4)
          .map(([k, v]) => `${k}×${v}`).join(", ");
        body += ` <span class="autofix-detail">[${top}]</span>`;
      }
    } else if (t === "chapter_hook") {
      icon = "🎣";
      label = "章末钩子重写";
      body = `${escapeAttr((d.before || "").slice(0, 30))}… → ${escapeAttr((d.after || "").slice(0, 30))}…`;
    } else if (t === "editor_patches") {
      icon = "✂️";
      label = "总编 patch 微改";
      body = `应用 ${d.applied || 0}/${d.total || 0}`;
      if (d.spin) body += ` (空转 ${d.spin})`;
    }
    return `<li class="autofix-item">
      <span class="autofix-icon">${icon}</span>
      <span class="autofix-ts">${entry.ts || ""}</span>
      <span class="autofix-label">${label}</span>
      <span class="autofix-body">${body}</span>
    </li>`;
  }).join("");
  return `<div class="editor-autofix">
    <div class="editor-autofix-title">🛠️ 代码自修轨迹(LLM 之前先按硬规则修)</div>
    <ul class="autofix-list">${items}</ul>
  </div>`;
}

function renderEditorScoreTrend() {
  const history = (appState && appState.editorScoreHistory) || [];
  if (!history.length) return "";
  const dots = history.map((h, i) => {
    const prev = i > 0 ? history[i - 1].score : null;
    const delta = prev != null ? h.score - prev : 0;
    const cls = prev == null ? "flat" : (delta > 0 ? "up" : (delta < 0 ? "down" : "flat"));
    const sign = delta > 0 ? `+${delta}` : `${delta}`;
    const arrow = prev == null ? "" : (delta > 0 ? "⬆" : (delta < 0 ? "⬇" : "→"));
    const tag = h.action === "approve" ? "✓过审" : (h.action === "reject" ? "✗驳回" : "打回");
    return `<span class="editor-trend-dot trend-${cls}" title="第 ${h.round} 轮 · ${tag} · ${h.ts || ''}">
      <b>${h.score}</b>${prev != null ? `<i>${arrow}${sign}</i>` : ""}
    </span>`;
  }).join('<span class="editor-trend-sep">→</span>');
  let summary = "";
  if (history.length >= 2) {
    const first = history[0].score;
    const last = history[history.length - 1].score;
    const total = last - first;
    if (total > 0) summary = `共涨 +${total} 分,重写有效`;
    else if (total < 0) summary = `共跌 ${total} 分,方向走偏`;
    else summary = `${history.length} 轮原地踏步,建议人工介入`;
  }
  return `<div class="editor-trend">
    <span class="editor-trend-label">审分历程:</span>
    ${dots}
    ${summary ? `<span class="editor-trend-summary">${summary}</span>` : ""}
  </div>`;
}

async function overrideEditor() {
  await mutate("/api/editor/override", {}, "人工通过总编审核");
}

async function reRunEditor() {
  await mutate("/api/editor/review", {}, "重新调用总编");
}

async function reRunScenes() {
  await mutate("/api/scenes/generate", {}, "重新生成本章正文");
}

async function rewriteByEditor() {
  await mutate("/api/editor/rewrite", {}, "按总编批注重写本章");
}

async function reRunDiagnostics() {
  await mutate("/api/editor/metrics", {}, "重新诊断章节去 AI 味");
}

window.overrideEditor = overrideEditor;
window.reRunEditor = reRunEditor;
window.reRunScenes = reRunScenes;
window.reRunDiagnostics = reRunDiagnostics;

function renderAll() {
  if (!appState) return;
  updateHeader();
  renderChapterList();
  renderWorkflow();
  renderQuickStart();
  renderBookOverview();
  renderWorkspace();
  renderMemory();
  renderHooks();
  renderCharacter();
  renderScores();
  renderStyleReport();
  renderTrace();
  renderLLMConfig();
  renderAgentSteps();
  renderAgentTimeline();
  renderEditorReviewPanel();
  renderIdeaDraft();
  renderProjectShelf();
  renderExportReminder();
}

// 导出提醒横幅:已归档但未导出章节 ≥ 5 时浮在顶部
function renderExportReminder() {
  const reminder = appState && appState.exportReminder;
  let bar = document.getElementById("exportReminderBar");
  if (!reminder || !reminder.active) {
    if (bar) bar.hidden = true;
    return;
  }
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "exportReminderBar";
    bar.className = "export-reminder";
    const app = document.querySelector(".app") || document.body;
    app.insertBefore(bar, app.firstChild);
  }
  const n = reminder.chaptersSinceLastExport;
  const lastNum = reminder.lastExportedChapter || 0;
  bar.hidden = false;
  bar.innerHTML = `
    <div class="export-reminder-msg">
      📥 已写完 <strong>${n}</strong> 章未导出${lastNum ? `(上次导出至第 ${lastNum} 章)` : ""}。建议立刻导出到本地,避免意外丢失。
    </div>
    <div class="export-reminder-actions">
      <button class="primary" id="exportReminderDownload">立即导出全书</button>
      <button id="exportReminderAck">我已导出 / 继续写</button>
    </div>`;
  document.getElementById("exportReminderDownload").addEventListener("click", async () => {
    await downloadExport("/api/export/book");
    // 后端在 GET 时已 bump lastExportedChapter,刷新一次 state
    try { const p = await API.getState(); appState = p.state; renderAll(); } catch (_) {}
  });
  document.getElementById("exportReminderAck").addEventListener("click", async () => {
    try {
      const p = await API.post("/api/export/acknowledge", {});
      appState = p.state;
      showToast(p.message || "已确认导出");
      renderAll();
    } catch (e) { showToast(e.message || "确认失败"); }
  });
}

async function runPrimaryWorkflow() {
  if (editorDirty) {
    const shouldSave = window.confirm("当前正文有未保存修改。是否先保存再继续？");
    if (!shouldSave) return;
    const saved = await saveCurrentScene();
    if (!saved) return;
  }
  const workflow = appState.workflow;
  if (!workflow) return;
  const endpoint = workflow.primaryEndpoint;
  if (workflow.primaryEndpoint === "/api/export/markdown") {
    downloadExport("/api/export/markdown");
    return;
  }
  const headerButton = document.getElementById("runWorkflowBtn");
  const panelButton = document.getElementById("workflowPrimaryBtn");
  setButtonBusy(headerButton, true, "正在处理...");
  setButtonBusy(panelButton, true, "正在处理...");
  try {
    const payload = await mutate("/api/workflow/next", {}, workflow.primaryLabel);
    if (payload && endpoint === "/api/audit/run") {
      switchView("audit");
    }
  } finally {
    if (headerButton) headerButton.disabled = false;
    if (panelButton) panelButton.disabled = false;
    renderWorkflow();
    renderQuickStart();
  }
}

async function runAutoChapter() {
  if (editorDirty) {
    const shouldSave = window.confirm("当前正文有未保存修改。是否先保存再继续？");
    if (!shouldSave) return;
    const saved = await saveCurrentScene();
    if (!saved) return;
  }
  const autoButton = document.getElementById("workflowAutoBtn");
  const primaryButton = document.getElementById("workflowPrimaryBtn");
  const headerButton = document.getElementById("runWorkflowBtn");
  setButtonBusy(autoButton, true, "正在自动写作...");
  if (primaryButton) primaryButton.disabled = true;
  if (headerButton) headerButton.disabled = true;
  try {
    for (let index = 0; index < 8; index += 1) {
      const workflow = appState.workflow;
      if (!workflow || workflow.currentStep === "export") {
        break;
      }
      const payload = await mutate("/api/workflow/next", {}, workflow.primaryLabel);
      if (!payload) {
        break;
      }
      appState = payload.state;
    }
    if (appState.workflow?.currentStep === "export") {
      showToast("本章已写完，可以导出或继续下一章");
      switchView("workspace");
    }
  } finally {
    setButtonBusy(autoButton, false);
    renderWorkflow();
    renderQuickStart();
  }
}

async function saveCurrentScene() {
  const scene = currentScene();
  if (!scene) return null;
  const editor = document.getElementById("chapterEditor");
  const content = editor.innerText.trim();
  if (!content) {
    showToast("正文不能为空");
    return null;
  }
  const button = document.getElementById("saveSceneBtn");
  setButtonBusy(button, true, "正在保存...");
  try {
    const payload = await mutate(`/api/scenes/${scene.id}/save`, { content }, "正文修改已保存");
    if (payload) {
      selectedSceneId = scene.id;
      editorDirty = false;
      updateSaveButton();
    }
    return payload;
  } finally {
    if (button) button.disabled = false;
    updateSaveButton();
  }
}

function bindEvents() {
  navItems.forEach((item) => {
    item.addEventListener("click", () => switchView(item.dataset.view));
  });

  document.querySelectorAll(".segment").forEach((segment) => {
    segment.addEventListener("click", () => {
      selectedMemory = segment.dataset.memory;
      document.querySelectorAll(".segment").forEach((item) => item.classList.toggle("active", item === segment));
      renderMemory();
    });
  });

  document.querySelectorAll(".person-node").forEach((node) => {
    node.addEventListener("click", () => {
      selectedCharacter = node.dataset.character;
      renderCharacter();
    });
  });

  document.getElementById("runWorkflowBtn").addEventListener("click", runPrimaryWorkflow);
  document.getElementById("workflowPrimaryBtn").addEventListener("click", runPrimaryWorkflow);
  document.getElementById("workflowAutoBtn").addEventListener("click", runAutoChapter);
  document.getElementById("planBtn").addEventListener("click", () => mutate("/api/plan/generate", {}, "Chapter Plan 已生成"));
  document.getElementById("lockPlanBtn").addEventListener("click", () => mutate("/api/plan/lock", {}, "计划已锁定"));
  document.getElementById("writeScenesBtn").addEventListener("click", () => mutate("/api/scenes/generate", {}, "场景已生成"));
  document.getElementById("saveSceneBtn").addEventListener("click", saveCurrentScene);
  document.getElementById("chapterEditor").addEventListener("input", markEditorDirty);

  document.getElementById("reviseSceneBtn").addEventListener("click", () => {
    const scene = currentScene();
    if (!scene) return;
    mutate(`/api/scenes/${scene.id}/revise`, {}, "场景已修订");
  });

  document.getElementById("auditBtn").addEventListener("click", async () => {
    await mutate("/api/audit/run", {}, "审计完成");
    switchView("audit");
  });

  document.getElementById("rerunAuditBtn").addEventListener("click", () => mutate("/api/audit/run", {}, "重新审计完成"));
  document.getElementById("styleEditBtn").addEventListener("click", () => mutate("/api/style/human-edit", {}, "原创表达审校完成"));
  document.getElementById("settleBtn").addEventListener("click", () => mutate("/api/truth/settle", {}, "TruthPatch 已写入"));
  document.getElementById("advanceHookBtn").addEventListener("click", () => mutate("/api/hooks/advance", {}, "伏笔已推进"));

  document.getElementById("llmConfigForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const formData = new FormData(form);
    const agents = {};
    (llmConfig?.agents || []).forEach((agent) => {
      agents[agent.id] = {
        baseUrl: String(formData.get(`${agent.id}_baseUrl`) || "").trim(),
        model: String(formData.get(`${agent.id}_model`) || "").trim(),
        apiKey: String(formData.get(`${agent.id}_apiKey`) || "").trim(),
      };
    });
    const body = {
      global: {
        apiKey: String(formData.get("apiKey") || "").trim(),
        baseUrl: String(formData.get("baseUrl") || "").trim(),
        model: String(formData.get("model") || "").trim(),
      },
      agents,
    };
    const submitButton = form.querySelector('button[type="submit"]');
    setButtonBusy(submitButton, true, "正在保存...");
    try {
      const payload = await mutate("/api/llm/config", body, "大模型配置已更新");
      if (payload) {
        llmConfig = await API.getLLMConfig().catch(() => llmConfig);
        llmStatus = await API.getLLMStatus().catch(() => llmStatus);
        form.reset();
        renderLLMConfig();
        renderQuickStart();
      }
    } finally {
      setButtonBusy(submitButton, false);
    }
  });

  document.getElementById("testLLMConfigBtn").addEventListener("click", async (event) => {
    const agentRole = document.getElementById("testAgentRole")?.value || "";
    const button = event.currentTarget;
    setButtonBusy(button, true, "正在测试...");
    try {
      const payload = await mutate("/api/llm/test", { agentRole }, "大模型连接成功");
      if (payload) {
        llmStatus = await API.getLLMStatus().catch(() => llmStatus);
        llmConfig = await API.getLLMConfig().catch(() => llmConfig);
        renderLLMConfig();
        renderQuickStart();
      }
    } finally {
      setButtonBusy(button, false);
    }
  });

  document.getElementById("exportBtn").addEventListener("click", () => {
    downloadExport("/api/export/markdown");
  });

  document.getElementById("exportBookBtn").addEventListener("click", () => {
    downloadExport("/api/export/book");
  });

  document.getElementById("bookExportBtn").addEventListener("click", () => {
    downloadExport("/api/export/book");
  });

  document.getElementById("bookContinueBtn").addEventListener("click", () => switchView("workspace"));

  document.getElementById("projectShelfBtn").addEventListener("click", loadAndOpenProjectShelf);
  document.getElementById("closeProjectShelfModal").addEventListener("click", closeProjectShelfModal);
  document.getElementById("projectShelfModal").addEventListener("click", (event) => {
    if (event.target.id === "projectShelfModal") closeProjectShelfModal();
  });
  document.getElementById("shelfNewIdeaBtn").addEventListener("click", () => {
    closeProjectShelfModal();
    openIdeaModal();
    document.querySelector('#ideaForm textarea[name="topic"]')?.focus();
  });
  document.getElementById("shelfManualBtn").addEventListener("click", () => {
    closeProjectShelfModal();
    openNewProjectModal();
  });

  document.getElementById("finishExportBtn").addEventListener("click", () => {
    downloadExport("/api/export/markdown");
  });

  document.getElementById("finishNextChapterBtn").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    setButtonBusy(button, true, "正在进入下一章...");
    try {
      const payload = await mutate("/api/chapters/next", {}, "已进入下一章");
      if (payload) {
        selectedSceneId = payload.state.scenes[0]?.id || null;
        switchView("workspace");
      }
    } finally {
      setButtonBusy(button, false);
    }
  });

  document.getElementById("finishNewIdeaBtn").addEventListener("click", () => {
    openIdeaModal();
    document.querySelector('#ideaForm textarea[name="topic"]')?.focus();
  });

  document.getElementById("quickManualBtn").addEventListener("click", () => {
    const quickLength = document.getElementById("quickStoryLength")?.value || "long";
    const manualLength = document.querySelector('#newProjectForm select[name="storyLength"]');
    if (manualLength) manualLength.value = quickLength;
    openNewProjectModal();
  });

  document.getElementById("quickIdeaBtn").addEventListener("click", async (event) => {
    const input = document.getElementById("quickTopicInput");
    const storyLength = document.getElementById("quickStoryLength")?.value || "long";
    const topic = input.value.trim();
    if (!topic) {
      showToast("先输入一个题材或脑洞");
      input.focus();
      return;
    }
    const button = event.currentTarget;
    setButtonBusy(button, true, "正在生成书案...");
    try {
      const payload = await mutate("/api/ideation/generate", { topic, platform: "fanqie", storyLength }, "题材书案已生成");
      if (payload) {
        const modalTopic = document.querySelector('#ideaForm textarea[name="topic"]');
        if (modalTopic) modalTopic.value = topic;
        const modalLength = document.querySelector('#ideaForm select[name="storyLength"]');
        if (modalLength) modalLength.value = storyLength;
        openIdeaModal();
        renderIdeaDraft();
      }
    } finally {
      setButtonBusy(button, false);
    }
  });

  document.getElementById("newProjectBtn").addEventListener("click", openNewProjectModal);
  document.getElementById("ideaProjectBtn").addEventListener("click", openIdeaModal);
  document.getElementById("closeProjectModal").addEventListener("click", closeNewProjectModal);
  document.getElementById("cancelProjectCreate").addEventListener("click", closeNewProjectModal);
  document.getElementById("closeIdeaModal").addEventListener("click", closeIdeaModal);
  document.getElementById("cancelIdeaCreate").addEventListener("click", closeIdeaModal);
  document.getElementById("closeChapterReaderModal").addEventListener("click", closeChapterReaderModal);
  document.getElementById("chapterReaderModal").addEventListener("click", (event) => {
    if (event.target.id === "chapterReaderModal") closeChapterReaderModal();
  });
  document.getElementById("readerExportBtn").addEventListener("click", (event) => {
    const chapter = event.currentTarget.dataset.chapter;
    if (chapter) downloadExport(`/api/export/markdown?chapter=${chapter}`);
  });
  document.getElementById("newProjectModal").addEventListener("click", (event) => {
    if (event.target.id === "newProjectModal") closeNewProjectModal();
  });
  document.getElementById("ideaProjectModal").addEventListener("click", (event) => {
    if (event.target.id === "ideaProjectModal") closeIdeaModal();
  });

  document.getElementById("newProjectForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const formData = new FormData(form);
    const body = Object.fromEntries(formData.entries());
    const submitButton = form.querySelector('button[type="submit"]');
    setButtonBusy(submitButton, true, "正在创建...");
    try {
      const payload = await mutate("/api/projects/create", body, "新书已创建");
      if (payload) {
        resetWorkspaceSelection(payload.state);
        projectList = await API.getProjects().catch(() => projectList);
        closeNewProjectModal();
        switchView("workspace");
        renderAll();
      }
    } finally {
      setButtonBusy(submitButton, false);
    }
  });

  document.getElementById("ideaForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const formData = new FormData(form);
    const body = Object.fromEntries(formData.entries());
    const submitButton = form.querySelector('button[type="submit"]');
    setButtonBusy(submitButton, true, "正在生成书案...");
    try {
      const payload = await mutate("/api/ideation/generate", body, "题材书案已生成");
      if (payload) {
        openIdeaModal();
        renderIdeaDraft();
      }
    } finally {
      setButtonBusy(submitButton, false);
    }
  });

  document.getElementById("adoptIdeaBtn").addEventListener("click", async (event) => {
    const idea = pendingIdeaDraft();
    if (!idea) {
      showToast("请先生成题材书案");
      return;
    }
    if (!idea.confirmed) {
      showToast("请先勾选「我已通读并确认」");
      return;
    }
    const button = event.currentTarget;
    setButtonBusy(button, true, "正在创建...");
    try {
      const payload = await mutate("/api/projects/create-from-idea", { ideaDraft: idea }, "已采用书案创建作品");
      if (payload) {
        resetWorkspaceSelection(payload.state);
        projectList = await API.getProjects().catch(() => projectList);
        closeIdeaModal();
        switchView("workspace");
        renderAll();
      }
    } finally {
      setButtonBusy(button, false);
    }
  });

  const confirmCheckbox = document.getElementById("ideaConfirmCheckbox");
  if (confirmCheckbox) {
    confirmCheckbox.addEventListener("change", async (event) => {
      const idea = pendingIdeaDraft();
      if (!idea) {
        showToast("请先生成题材书案");
        event.target.checked = false;
        return;
      }
      await mutate("/api/ideation/confirm", { confirmed: event.target.checked });
    });
  }
}

async function init() {
  bindEvents();
  // === 登录门: 优先消费 URL 上的 ?token=,然后查 /api/auth/status ===
  try {
    const qp = new URLSearchParams(window.location.search);
    const incoming = qp.get("token");
    if (incoming) {
      try { window.localStorage.setItem(window.QJ_TOKEN_KEY, incoming); } catch (_) {}
      qp.delete("token");
      const clean = window.location.pathname + (qp.toString() ? "?" + qp.toString() : "") + window.location.hash;
      window.history.replaceState({}, "", clean);
    }
  } catch (_) {}

  let authed = false;
  let authedEmail = null;
  try {
    const ar = await fetch("/api/auth/status");
    const aj = await ar.json();
    authed = !!aj.authenticated;
    authedEmail = aj.email || null;
  } catch (_) {
    authed = false;
  }
  // 临时:鉴权拦截已取消, 前端登录门也强制放行 (2026-05-24)
  authed = true;
  if (!authed) {
    window.qjRenderAuthGate(true);
    return;
  }
  window.qjRenderAuthGate(false, authedEmail);

  try {
    const [payload, modelStatus, modelConfig, projects] = await Promise.all([
      API.getState(),
      API.getLLMStatus().catch(() => null),
      API.getLLMConfig().catch(() => null),
      API.getProjects().catch(() => []),
    ]);
    appState = payload.state;
    llmStatus = modelStatus;
    llmConfig = modelConfig;
    projectList = projects;
    selectedSceneId = appState.scenes[0]?.id || null;
    renderAll();
  } catch (error) {
    showToast(`加载失败：${error.message}`);
  }
}

// === 登录遮罩: 未登录时盖在 .app 之上,引导跳主站 SSO ===
// 自动同步:监听 storage 事件 + 切回标签时重测一次,主站登录后无需刷新即可解锁
let _qjAuthCheckInFlight = false;
async function qjRecheckAuth() {
  if (_qjAuthCheckInFlight) return;
  _qjAuthCheckInFlight = true;
  try {
    const r = await fetch("/api/auth/status");
    const j = await r.json();
    if (j.authenticated) {
      window.qjRenderAuthGate(false, j.email);
      // 第一次解锁需要把主数据拉一次
      if (!appState) {
        try {
          const [payload, modelStatus, modelConfig, projects] = await Promise.all([
            API.getState(),
            API.getLLMStatus().catch(() => null),
            API.getLLMConfig().catch(() => null),
            API.getProjects().catch(() => []),
          ]);
          appState = payload.state;
          llmStatus = modelStatus;
          llmConfig = modelConfig;
          projectList = projects;
          selectedSceneId = appState.scenes[0]?.id || null;
          renderAll();
          showToast(`已识别到登录,欢迎 ${j.email || ""}`);
        } catch (e) { showToast("加载失败: " + e.message); }
      }
    }
  } catch (_) {}
  _qjAuthCheckInFlight = false;
}

window.qjRenderAuthGate = function (masked, email) {
  let gate = document.getElementById("authGate");
  if (masked) {
    if (!gate) {
      gate = document.createElement("div");
      gate.id = "authGate";
      gate.className = "auth-gate";
      gate.innerHTML = `
        <div class="auth-gate-card">
          <div class="auth-gate-title">🔒 请先登录 AI 秘密基地</div>
          <div class="auth-gate-sub">千卷已并入 AI 秘密基地账号体系。如果你已在主站登录,通常会自动识别;如未识别,可点「诊断」查看原因。</div>
          <div class="auth-gate-row">
            <button id="authGateGoLogin" class="primary auth-gate-btn">前往主站登录</button>
            <button id="authGateRetry" class="auth-gate-btn-ghost">我已登录,重试</button>
            <button id="authGateDiag" class="auth-gate-btn-ghost">诊断</button>
          </div>
          <div id="authGateDiagPanel" class="auth-gate-diag" hidden>
            <div class="auth-gate-diag-row">
              <span class="auth-gate-diag-label">localStorage["ec_ai_token"]:</span>
              <span id="authGateDiagToken" class="auth-gate-diag-value">-</span>
            </div>
            <div class="auth-gate-diag-row">
              <span class="auth-gate-diag-label">主站 /api/auth/me:</span>
              <span id="authGateDiagMe" class="auth-gate-diag-value">-</span>
            </div>
            <div class="auth-gate-diag-row">
              <span class="auth-gate-diag-label">千卷 /api/auth/status:</span>
              <span id="authGateDiagStatus" class="auth-gate-diag-value">-</span>
            </div>
            <div class="auth-gate-diag-help">
              如果 localStorage 显示「无」,说明主站没有登录(或登录的不是同一个域名 / 浏览器);
              如果主站 /api/auth/me 返回用户但千卷 /api/auth/status 显示未登录,是后端密钥不匹配,联系管理员。
            </div>
            <div class="auth-gate-diag-paste">
              <label class="auth-gate-diag-label">手动粘贴 token(应急,在主站 DevTools → Application → Local Storage → 复制 ec_ai_token 的值):</label>
              <textarea id="authGatePasteToken" rows="3" placeholder="粘贴 ec_ai_token 到这里"></textarea>
              <button id="authGatePasteApply" class="auth-gate-btn-ghost">应用并解锁</button>
            </div>
          </div>
          <div class="auth-gate-hint">主站登录后切回本标签会自动重测,不用刷新。</div>
        </div>`;
      document.body.appendChild(gate);
      document.getElementById("authGateGoLogin").addEventListener("click", function () {
        // 新标签页打开,保留当前千卷页面;主站登录后切回本页自动 recheck
        window.open(window.QJ_LOGIN_URL, "_blank", "noopener");
      });
      document.getElementById("authGateRetry").addEventListener("click", function () {
        qjRecheckAuth();
      });
      document.getElementById("authGateDiag").addEventListener("click", async function () {
        const panel = document.getElementById("authGateDiagPanel");
        panel.hidden = !panel.hidden;
        if (panel.hidden) return;
        // 1) localStorage token
        const t = window.qjGetAuthToken();
        document.getElementById("authGateDiagToken").textContent =
          t ? `存在 (长度 ${t.length}, 前缀 ${t.slice(0, 16)}..)` : "无 — 你尚未在本浏览器同源下登录";
        // 2) 主站 /api/auth/me (用原始 fetch 绕过千卷子路径前缀,直达主站 Express)
        try {
          const headers = t ? { Authorization: "Bearer " + t } : {};
          const r = await (window._origRawFetch || fetch)("/api/auth/me", { headers });
          const txt = await r.text();
          document.getElementById("authGateDiagMe").textContent =
            r.ok ? `200 OK — ${txt.slice(0, 80)}...` : `${r.status} — ${txt.slice(0, 120)}`;
        } catch (e) {
          document.getElementById("authGateDiagMe").textContent = "请求失败: " + e.message;
        }
        // 3) 千卷 /api/auth/status
        try {
          const r = await fetch("/api/auth/status");
          const j = await r.json();
          document.getElementById("authGateDiagStatus").textContent =
            j.authenticated ? `已识别: ${j.email}` : "未识别 (authenticated=false)";
        } catch (e) {
          document.getElementById("authGateDiagStatus").textContent = "请求失败: " + e.message;
        }
      });
      document.getElementById("authGatePasteApply").addEventListener("click", function () {
        const v = (document.getElementById("authGatePasteToken").value || "").trim();
        if (!v) { showToast("token 不能为空"); return; }
        try {
          window.localStorage.setItem(window.QJ_TOKEN_KEY, v);
          qjRecheckAuth();
        } catch (e) { showToast("写入失败: " + e.message); }
      });
      // 跨标签同步:另一个标签 setItem('ec_ai_token', ...) 时本标签收到事件
      window.addEventListener("storage", function (e) {
        if (e.key === window.QJ_TOKEN_KEY && e.newValue) {
          qjRecheckAuth();
        }
      });
      // 切回标签 / 窗口聚焦 → 重测一次
      document.addEventListener("visibilitychange", function () {
        if (!document.hidden && document.getElementById("authGate") && !document.getElementById("authGate").hidden) {
          qjRecheckAuth();
        }
      });
      window.addEventListener("focus", function () {
        const g = document.getElementById("authGate");
        if (g && !g.hidden) qjRecheckAuth();
      });
    }
    gate.hidden = false;
  } else {
    if (gate) gate.hidden = true;
    // 在顶栏右侧露出 email + 退出
    const slot = document.getElementById("authBadge") || (function () {
      const el = document.createElement("div");
      el.id = "authBadge";
      el.className = "auth-badge";
      document.body.appendChild(el);
      return el;
    })();
    if (email) {
      slot.innerHTML = `<span class="auth-badge-email" title="${email}">${email}</span><button id="authBadgeLogout" class="auth-badge-logout">退出</button>`;
      const btn = document.getElementById("authBadgeLogout");
      if (btn) btn.addEventListener("click", function () {
        window.qjClearAuthToken();
        window.location.reload();
      });
    }
  }
};

init();
// === Progress overlay for long-running endpoints ===
(function () {
  const LABELS = {
    '/api/ideation/generate': {title: '正在生成题材构思',     sub: 'AI 在脑暴 3 个候选方向,通常 10-30 秒'},
    '/api/ideation/regenerate-candidates': {title: '正在重新生成 3 个候选', sub: '依据当前流派重新展开,通常 20-40 秒'},
    '/api/ideation/refine':   {title: '正在打磨题材',         sub: '按你的方向修订当前候选,通常 10-20 秒'},
    '/api/ideation/patch':    {title: '正在按指令编辑',       sub: '按你的批注微调,通常 5-15 秒'},
    '/api/ideation/confirm':  {title: '正在确认题材',         sub: '处理中...'},
    '/api/ideation/set-genre':{title: '正在切换流派',         sub: '处理中...'},
    '/api/projects/create-from-idea': {title: '正在创建项目', sub: '准备故事档案,通常 5-10 秒'},
    '/api/plan/lock':         {title: '正在锁定章节计划',     sub: '展开本章场景骨架,通常 10-30 秒'},
    '/api/scenes/generate':   {title: '正在生成章节正文',     sub: 'AI 正在写 2000+ 字,通常 30-90 秒'},
    '/api/scenes/finalize':   {title: '正在锁定章节',         sub: '把章节存档,准备下一章'},
    '/api/audit/run':         {title: '正在审计章节',         sub: '检查连续性 / 体验 / 风格,通常 10-30 秒'},
    '/api/editor/review':     {title: '总编正在审稿',         sub: '不达标会自动重写,最多 3 轮,通常 30 秒 - 3 分钟'},
    '/api/editor/override':   {title: '记录人工通过',         sub: '处理中...'},
    '/api/editor/rewrite':    {title: '正在按总编批注重写',   sub: '正文写手回炉再造,通常 30-90 秒'},
    '/api/revise/auto':       {title: '正在按审计建议修订',   sub: 'AI 调整问题段落,通常 10-20 秒'},
    '/api/style/run':         {title: '正在审校原创表达',     sub: '减少模板化和 AI 腔,通常 10-30 秒'},
    '/api/style/human-edit':  {title: '正在精修文笔',         sub: '深度打磨语言,通常 15-40 秒'},
    '/api/truth/settle':      {title: '正在归档真相',         sub: '从正文抽取新事实进 canon,通常 10-30 秒'},
  };
  let busyCount = 0;
  let startTime = 0;
  let timerId = null;
  let warnTimer = null;
  function _byId(id){ return document.getElementById(id); }
  function fmtElapsed(ms){
    const s = Math.floor(ms/1000);
    if (s < 60) return s + 's';
    const m = Math.floor(s/60), r = s%60;
    return m + 'm ' + r + 's';
  }
  function show(label){
    busyCount++;
    const ov = _byId('qjBusyOverlay'); if (!ov) return;
    if (busyCount === 1) {
      _byId('qjBusyTitle').textContent = label.title || '处理中';
      _byId('qjBusySub').textContent   = label.sub   || 'AI 正在思考,请稍候';
      _byId('qjBusyElapsed').textContent = '0s';
      _byId('qjBusyHint').textContent = label.hint || '可以喝口水等等';
      _byId('qjBusyWarn').hidden = true;
      ov.hidden = false;
      startTime = Date.now();
      timerId = setInterval(function(){
        _byId('qjBusyElapsed').textContent = fmtElapsed(Date.now() - startTime);
      }, 250);
      if (warnTimer) clearTimeout(warnTimer);
      warnTimer = setTimeout(function(){
        const el = _byId('qjBusyWarn'); if (el) el.hidden = false;
      }, 90000);
    }
  }
  function hide(){
    busyCount = Math.max(0, busyCount - 1);
    if (busyCount === 0) {
      const ov = _byId('qjBusyOverlay'); if (ov) ov.hidden = true;
      if (timerId) { clearInterval(timerId); timerId = null; }
      if (warnTimer) { clearTimeout(warnTimer); warnTimer = null; }
    }
  }
  window.qjBusyShow = show;
  window.qjBusyHide = hide;

  // 包一层 fetch:对任何 POST /api/* 自动弹 overlay
  const prevFetch = window.fetch.bind(window);
  window.fetch = function(input, init){
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const method = ((init && init.method) || (typeof input === 'object' && input && input.method) || 'GET').toString().toUpperCase();
    let shown = false;
    if (method === 'POST' && url.indexOf('/api/') === 0) {
      const key = url.split('?')[0];
      const label = LABELS[key] || {title: '处理中', sub: 'AI 正在处理你的请求'};
      show(label);
      shown = true;
    }
    return prevFetch(input, init).then(function(resp){
      if (shown) hide();
      return resp;
    }, function(err){
      if (shown) hide();
      throw err;
    });
  };
})();
// === END progress overlay ===


