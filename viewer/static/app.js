import { httpDataSource } from "./http-data.js";
import { createRouteNavigation } from "./navigation.js";

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("./service-worker.js").catch(() => {
      // The viewer remains usable as a normal website if registration fails.
    });
  });
}

const app = document.querySelector("#app");
const sidebar = document.querySelector("#sidebar");
const drawerBackdrop = document.querySelector("#drawer-backdrop");
const mobileMenuButton = document.querySelector('[data-action="toggle-files"]');
const pickerView = document.querySelector("#picker-view");
const readerView = document.querySelector("#reader-view");
const languageList = document.querySelector("#language-list");
const sidebarLanguage = document.querySelector("#sidebar-language");
const fileNav = document.querySelector("#file-nav");
const readerKicker = document.querySelector("#reader-kicker");
const readerTitle = document.querySelector("#reader-title");
const readerPath = document.querySelector("#reader-path");
const readerLoading = document.querySelector("#reader-loading");
const readerError = document.querySelector("#reader-error");
const documentContent = document.querySelector("#document-content");
const themeToggles = document.querySelectorAll("[data-theme-toggle]");
const colorSchemeQuery = typeof window.matchMedia === "function"
  ? window.matchMedia("(prefers-color-scheme: dark)")
  : null;

const state = {
  dataSource: httpDataSource,
  languages: [],
  languageId: null,
  languageLabel: null,
  groups: [],
  currentPath: null,
  openGroups: {},
  theme: readTheme(),
};

const routeNavigation = createRouteNavigation(state.dataSource, applyRouteResult);

function readTheme() {
  try {
    const savedTheme = localStorage.getItem("linguamcp-theme");
    return ["system", "light", "dark"].includes(savedTheme) ? savedTheme : "system";
  } catch {
    return "system";
  }
}

function resolvedTheme(theme) {
  return theme === "system" && colorSchemeQuery?.matches ? "dark" : theme === "system" ? "light" : theme;
}

function setTheme(theme, { persist = true } = {}) {
  state.theme = ["system", "light", "dark"].includes(theme) ? theme : "system";
  const activeTheme = resolvedTheme(state.theme);
  document.documentElement.dataset.theme = state.theme;
  themeToggles.forEach((toggle) => {
    const isDark = activeTheme === "dark";
    toggle.dataset.theme = activeTheme;
    toggle.setAttribute("aria-checked", String(isDark));
    toggle.setAttribute("aria-label", isDark ? "Switch to light mode" : "Switch to dark mode");
  });
  if (persist) {
    try {
      localStorage.setItem("linguamcp-theme", state.theme);
    } catch {
      // The preview still works when storage is disabled.
    }
  }
}

function toggleTheme() {
  setTheme(resolvedTheme(state.theme) === "dark" ? "light" : "dark");
}

function setDrawerButtonState(isOpen) {
  if (!mobileMenuButton) return;
  mobileMenuButton.setAttribute("aria-expanded", String(isOpen));
  mobileMenuButton.setAttribute("aria-label", isOpen ? "Close files" : "Open files");
}

function escapeHTML(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatPath(path) {
  return path.split("/").map(escapeHTML).join("/");
}

function encodePath(languageId, relativePath = "") {
  const encodedLanguage = encodeURIComponent(languageId);
  if (!relativePath) return `#/${encodedLanguage}`;
  const encodedPath = relativePath.split("/").map((segment) => encodeURIComponent(segment)).join("/");
  return `#/${encodedLanguage}/${encodedPath}`;
}

function decodeHash() {
  const value = window.location.hash.replace(/^#\/?/, "");
  if (!value) return { languageId: null, path: null };
  const segments = value.split("/").filter(Boolean).map((segment) => {
    try {
      return decodeURIComponent(segment);
    } catch {
      return segment;
    }
  });
  return {
    languageId: segments.shift() || null,
    path: segments.join("/") || null,
  };
}

function navigate(languageId, relativePath = "") {
  window.location.hash = encodePath(languageId, relativePath).slice(1);
}

function flattenDocuments(groups) {
  return groups.flatMap((group) => group.documents);
}

function findLanguage(languageId) {
  return state.languages.find((language) => language.id === languageId);
}

function firstDocument(groups) {
  return flattenDocuments(groups)[0] || null;
}

function setReaderState({ loading = false, error = "" } = {}) {
  readerLoading.hidden = !loading;
  readerError.hidden = !error;
  readerError.textContent = error;
  documentContent.hidden = loading || Boolean(error);
}

function renderLanguagePicker() {
  app.classList.add("is-picker");
  app.classList.remove("is-drawer-open");
  drawerBackdrop.hidden = true;
  drawerBackdrop.setAttribute("aria-hidden", "true");
  setDrawerButtonState(false);
  sidebar.setAttribute("aria-hidden", "true");
  pickerView.hidden = false;
  readerView.hidden = true;
  document.title = "LinguaMCP · Learner Memory";

  if (!state.languages.length) {
    languageList.innerHTML = `<div class="reader-state"><span>No language workspaces found.</span></div>`;
    return;
  }

  languageList.innerHTML = state.languages.map((language) => `
    <button class="language-card" type="button" data-language="${escapeHTML(language.id)}">
      <span class="language-card-name">${escapeHTML(language.label)}</span>
      <span class="language-card-action"><span>Open learner memory</span><span aria-hidden="true">→</span></span>
    </button>
  `).join("");
}

function renderSidebar() {
  sidebar.setAttribute("aria-hidden", "false");
  sidebarLanguage.innerHTML = `
    <p class="sidebar-language-label">Language</p>
    <p class="sidebar-language-name">${escapeHTML(state.languageLabel || "Learner memory")}</p>
  `;

  fileNav.innerHTML = state.groups.map((group) => {
    const isOpen = state.openGroups[group.id] ?? group.openByDefault ?? group.id === "current";
    const files = group.documents.map((document) => {
      const active = document.path === state.currentPath;
      return `
        <button class="nav-file${active ? " is-active" : ""}" type="button" data-file-path="${escapeHTML(document.path)}"${active ? ' aria-current="page"' : ""}>
          <span class="nav-file-label">${escapeHTML(document.label)}</span>
          <span class="nav-file-meta">${formatPath(document.path)}</span>
        </button>
      `;
    }).join("");
    const empty = `<p class="nav-empty">Nothing here yet.</p>`;
    return `
      <section class="nav-group${isOpen ? " is-open" : ""}" data-group="${escapeHTML(group.id)}">
        <button class="nav-group-heading" type="button" data-group-toggle="${escapeHTML(group.id)}" aria-expanded="${isOpen}">
          <span>${escapeHTML(group.label)}</span>
        </button>
        <div class="nav-group-list">${files || empty}</div>
      </section>
    `;
  }).join("");
}

function applyRouteResult(result) {
  if (result.kind === "picker") {
    renderLanguagePicker();
    return;
  }

  if (result.kind === "loading") {
    const languageChanged = state.languageId !== result.language.id;
    if (languageChanged) state.openGroups = {};
    app.classList.remove("is-picker");
    pickerView.hidden = true;
    readerView.hidden = false;
    state.languageId = result.language.id;
    state.languageLabel = result.language.label;
    state.groups = [];
    state.currentPath = result.requestedPath || null;
    setReaderState({ loading: true });
    readerKicker.textContent = `${result.language.label} · Learner memory`;
    readerTitle.textContent = "Opening…";
    readerPath.textContent = result.requestedPath || "";
    documentContent.innerHTML = "";
    renderSidebar();
    return;
  }

  if (result.kind === "missing" || result.kind === "empty") {
    state.groups = result.groups;
    state.currentPath = null;
    renderSidebar();
    const missingDocument = result.kind === "missing";
    setReaderState({ error: missingDocument ? "This Markdown document could not be found." : "This language has no Markdown documents yet." });
    readerTitle.textContent = missingDocument ? "Document not found" : "No documents yet";
    readerPath.textContent = "";
    document.title = `${result.language.label} · LinguaMCP`;
    return;
  }

  if (result.kind === "document") {
    state.groups = result.groups;
    state.currentPath = result.target.path;
    if (result.targetGroup) state.openGroups[result.targetGroup] = true;
    renderSidebar();
    readerTitle.textContent = result.documentData.label;
    readerPath.textContent = result.documentData.path;
    document.title = `${result.documentData.label} · ${result.language.label} · LinguaMCP`;
    documentContent.innerHTML = result.documentData.html;
    setReaderState();
    return;
  }

  if (result.kind === "error") {
    readerTitle.textContent = "Unable to open document";
    readerPath.textContent = result.requestedPath || "";
    setReaderState({ error: result.error.message === "document_not_found" ? "This Markdown document could not be found." : "This document could not be opened in the preview." });
  }
}

function renderRoute() {
  const { languageId, path } = decodeHash();
  return routeNavigation.render(languageId, path, state.languages);
}

function closeDrawer() {
  app.classList.remove("is-drawer-open");
  drawerBackdrop.hidden = true;
  drawerBackdrop.setAttribute("aria-hidden", "true");
  sidebar.setAttribute("aria-hidden", "false");
  setDrawerButtonState(false);
}

function openDrawer() {
  if (window.innerWidth >= 900 || app.classList.contains("is-picker")) return;
  app.classList.add("is-drawer-open");
  drawerBackdrop.hidden = false;
  drawerBackdrop.setAttribute("aria-hidden", "false");
  setDrawerButtonState(true);
}

function toggleDrawer() {
  if (app.classList.contains("is-drawer-open")) {
    closeDrawer();
  } else {
    openDrawer();
  }
}

document.addEventListener("click", (event) => {
  const themeToggle = event.target.closest("[data-theme-toggle]");
  if (themeToggle) {
    toggleTheme();
    return;
  }

  const action = event.target.closest("[data-action]")?.dataset.action;
  if (action === "back-to-languages") {
    window.location.hash = "";
    return;
  }
  if (action === "toggle-files") {
    toggleDrawer();
    return;
  }

  const languageCard = event.target.closest("[data-language]");
  if (languageCard) {
    const languageId = languageCard.dataset.language;
    state.dataSource.listDocuments(languageId).then((groups) => {
      const first = firstDocument(groups);
      navigate(languageId, first?.path || "");
    });
    return;
  }

  const groupButton = event.target.closest("[data-group-toggle]");
  if (groupButton) {
    const groupId = groupButton.dataset.groupToggle;
    state.openGroups[groupId] = !state.openGroups[groupId];
    renderSidebar();
    return;
  }

  const fileButton = event.target.closest("[data-file-path]");
  if (fileButton) {
    navigate(state.languageId, fileButton.dataset.filePath);
    closeDrawer();
  }
});

drawerBackdrop.addEventListener("click", closeDrawer);
window.addEventListener("hashchange", renderRoute);
window.addEventListener("resize", () => {
  if (window.innerWidth >= 900) closeDrawer();
});

if (colorSchemeQuery) {
  const refreshSystemTheme = () => {
    if (state.theme === "system") setTheme("system", { persist: false });
  };
  if (typeof colorSchemeQuery.addEventListener === "function") {
    colorSchemeQuery.addEventListener("change", refreshSystemTheme);
  } else if (typeof colorSchemeQuery.addListener === "function") {
    colorSchemeQuery.addListener(refreshSystemTheme);
  }
}

setTheme(state.theme);

state.dataSource.listLanguages().then((languages) => {
  state.languages = languages;
  renderRoute();
}).catch(() => {
  state.languages = [];
  renderLanguagePicker();
});
