// === BEGIN qianjuan: base path + fetch interceptor ===
// 让前端在 "/" 根路径(本地 dev)和 "/toolbox/qianjuan/" 子路径(生产)下都能工作
(function () {
  const pathMatch = window.location.pathname.match(/^(\/toolbox\/[^/]+)\//);
  window.QJ_BASE = pathMatch ? pathMatch[1] : '';
  const _origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    if (typeof input === 'string' && input.startsWith('/api/')) {
      return _origFetch(window.QJ_BASE + input, init);
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
      window.location.href = `/api/export/markdown?chapter=${button.dataset.chapter}`;
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
  document.getElementById("ideaSellingPoint").textContent = idea.sellingPoint;
  document.getElementById("ideaWorld").textContent = idea.worldSetting;
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
  renderIdeaDraft();
  renderProjectShelf();
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
    window.location.href = "/api/export/markdown";
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
    window.location.href = "/api/export/markdown";
  });

  document.getElementById("exportBookBtn").addEventListener("click", () => {
    window.location.href = "/api/export/book";
  });

  document.getElementById("bookExportBtn").addEventListener("click", () => {
    window.location.href = "/api/export/book";
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
    window.location.href = "/api/export/markdown";
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
    if (chapter) window.location.href = `/api/export/markdown?chapter=${chapter}`;
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
}

async function init() {
  bindEvents();
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

init();
