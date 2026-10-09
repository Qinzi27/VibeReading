/* VibeReading: local source inspection, conservative call maps, and versioned notes. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SVG_NS = "http://www.w3.org/2000/svg";
  const MAX_GRAPH_NODES = 48;
  const NOTE_FIELDS = ["summary", "inputs", "outputs", "notes", "origin"];
  const state = {
    graph: null, selectedId: null, selectedSnapshot: null, scope: "all",
    search: "", language: "", tag: null, zoom: 1, tab: "source",
    drafts: new Map(), form: null, polling: false, switching: false,
    saving: false, fetchSequence: 0, lastConnectionError: false,
  };

  // All source, comments, file paths, and error strings are inserted as text.
  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = String(content);
    return node;
  }

  function svgElement(tag, attrs, content) {
    const node = document.createElementNS(SVG_NS, tag);
    Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (content !== undefined) node.textContent = String(content);
    return node;
  }

  function truncate(value, length) {
    const text = String(value || "");
    return text.length > length ? text.slice(0, length - 1) + "…" : text;
  }

  function notify(message, isError = false) {
    $("notice-text").textContent = message;
    $("notice").classList.toggle("error", isError);
    $("notice").hidden = false;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin", cache: "no-store", ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    let body;
    try { body = await response.json(); }
    catch (_) { throw new Error("本地服务返回了无法读取的响应，请检查服务终端。"); }
    if (!response.ok) {
      const error = new Error(body.error || `请求失败（HTTP ${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return body;
  }

  function post(path, body) {
    return api(path, { method: "POST", body: JSON.stringify(body) });
  }

  function graphNodes() { return state.graph?.nodes || []; }
  function graphEdges() { return state.graph?.edges || []; }
  function currentNode() { return graphNodes().find((node) => node.id === state.selectedId); }
  function projectKey() { return state.graph?.project?.root || ""; }
  function draftKey(id) { return `${projectKey()}\u0000${id}`; }
  function annotationVersion(node) { return Number(node?.annotation?.version || 0); }
  function tagMatches(node) { return !state.tag || (node.file === state.tag.file && node.container === state.tag.container); }
  function isActiveTag(node) { return Boolean(state.tag && node.file === state.tag.file && node.container === state.tag.container); }

  function filteredNodes() {
    const query = state.search.trim().toLocaleLowerCase();
    return graphNodes().filter((node) => {
      if (state.language && node.language !== state.language) return false;
      if (!tagMatches(node)) return false;
      return !query || [node.name, node.qualified_name, node.file, node.container, node.doc]
        .some((value) => String(value || "").toLocaleLowerCase().includes(query));
    });
  }

  function updateWatch(status, connectionError = false) {
    const error = connectionError || Boolean(status?.error);
    $("watch-status").classList.toggle("error", error);
    const time = status?.last_scan ? new Date(status.last_scan) : null;
    const readableTime = time && !Number.isNaN(time.getTime())
      ? time.toLocaleTimeString("zh-CN", { hour12: false }) : "";
    $("watch-text").textContent = connectionError ? "服务连接中断"
      : status?.error ? "读取遇到问题"
      : status?.watching ? "保存后自动刷新" : "正在读取项目";
    $("watch-status").title = connectionError ? "请查看启动服务的终端；页面会继续尝试连接。"
      : status?.error || (readableTime ? `最近扫描：${readableTime}` : "只监听已保存到磁盘的文件，不执行项目代码。");
  }

  function applyGraph(graph, newProject = false) {
    // Defensive defaults also support the server's initial revision=0 snapshot.
    graph.nodes ||= []; graph.edges ||= []; graph.files ||= [];
    graph.unresolved ||= []; graph.diagnostics ||= [];
    const previousProject = projectKey();
    state.graph = graph;
    if (newProject || previousProject !== projectKey()) {
      state.selectedId = null; state.selectedSnapshot = null;
      state.form = null; state.tag = null; state.search = ""; state.language = "";
      $("search").value = ""; state.scope = "all";
      $("project-path").value = graph.project?.root || "";
    } else if (!$("project-path").value) {
      $("project-path").value = graph.project?.root || "";
    }
    if (!state.selectedId && graph.nodes.length) state.selectedId = graph.nodes[0].id;
    $("project-name").textContent = graph.project?.name || "本地项目";
    $("project-name").title = graph.project?.root || "";
    $("file-count").textContent = graph.files.length;
    const languages = [...new Set([...(graph.languages || []), ...graph.nodes.map((node) => node.language)])].sort();
    $("language-filter").replaceChildren(element("option", "", "全部语言"));
    $("language-filter").firstChild.value = "";
    languages.forEach((language) => {
      const option = element("option", "", language); option.value = language;
      $("language-filter").append(option);
    });
    $("language-filter").value = state.language;
    updateWatch(graph);
    renderTree(); renderGraph(); renderInspector(); renderDiagnostics(); updateStats();
  }

  async function fetchGraph() {
    const sequence = ++state.fetchSequence;
    const graph = await api("/api/graph");
    if (sequence !== state.fetchSequence || state.switching) return;
    applyGraph(graph);
    state.lastConnectionError = false;
  }

  function updateStats() {
    const stats = state.graph?.stats || {};
    const drafts = [...state.drafts.keys()].filter((key) => key.startsWith(`${projectKey()}\u0000`)).length;
    const base = `${stats.files ?? state.graph?.files?.length ?? 0} 个文件 · ${stats.functions ?? graphNodes().length} 个函数 · ${stats.calls ?? graphEdges().length} 项调用 · ${stats.unresolved ?? state.graph?.unresolved?.length ?? 0} 项未解析`;
    $("project-stats").textContent = base + (drafts ? ` · ${drafts} 条未保存说明` : " · 仅读取源代码");
  }

  function activateTag(node) {
    if (!node.container) return;
    state.tag = isActiveTag(node) ? null : { file: node.file, container: node.container };
    state.scope = "all";
    renderTree(); renderGraph(); renderInspectorTags();
  }

  function makeContainerTag(node, compact = false) {
    const button = element("button", `${compact ? "tiny-tag" : "container-tag"}${isActiveTag(node) ? " active" : ""}`, `# ${node.container}`);
    button.type = "button";
    button.title = `筛选 ${node.file} 中的 ${node.container}；再次点击可清除`;
    button.setAttribute("aria-pressed", String(isActiveTag(node)));
    button.addEventListener("click", () => activateTag(node));
    return button;
  }

  function renderTree() {
    const tree = $("file-tree");
    const previousOpen = new Set([...tree.querySelectorAll("details[open]")].map((node) => node.dataset.path));
    const hadTree = tree.children.length > 0;
    tree.replaceChildren();
    const visible = filteredNodes();
    const byFile = new Map();
    visible.forEach((node) => {
      if (!byFile.has(node.file)) byFile.set(node.file, []);
      byFile.get(node.file).push(node);
    });
    // Show files without functions, including syntax-error files, in an unfiltered view.
    if (!state.search && !state.tag) {
      (state.graph?.files || []).forEach((file) => {
        if ((!state.language || file.language === state.language) && !byFile.has(file.path)) byFile.set(file.path, []);
      });
    }
    $("visible-count").textContent = `${visible.length} 函数`;
    $("active-tag").hidden = !state.tag;
    if (state.tag) {
      $("active-tag-text").textContent = `# ${state.tag.container}`;
      $("active-tag-text").title = state.tag.file;
    }
    if (!byFile.size) { tree.append(element("p", "empty-state", "没有匹配的文件或函数")); return; }
    [...byFile.keys()].sort().forEach((path) => {
      const nodes = byFile.get(path).sort((a, b) => a.line - b.line);
      const group = element("details", "file-group"); group.dataset.path = path;
      group.open = previousOpen.has(path) || nodes.some((node) => node.id === state.selectedId) || (!hadTree && byFile.size <= 10);
      const summary = element("summary"); summary.title = path;
      summary.append(element("span", "file-chevron", "›"), element("span", "file-icon", "▤"), element("span", "file-path", path), element("span", "count", nodes.length));
      group.append(summary);
      nodes.forEach((node) => {
        const row = element("div", `tree-function${node.id === state.selectedId ? " selected" : ""}`);
        const button = element("button", "function-select"); button.type = "button";
        button.title = `${node.qualified_name || node.name} · 第 ${node.line} 行`;
        button.setAttribute("aria-current", node.id === state.selectedId ? "true" : "false");
        button.append(element("span", "symbol", "ƒ"), element("span", "name", node.name));
        button.addEventListener("click", () => selectNode(node.id));
        row.append(button);
        if (node.container) row.append(makeContainerTag(node, true));
        group.append(row);
      });
      if (!nodes.length) {
        const file = (state.graph?.files || []).find((entry) => entry.path === path);
        group.append(element("div", "file-empty", file?.error ? "文件解析异常，见底部提示" : "未提取到函数定义"));
      }
      tree.append(group);
    });
  }

  function graphSelection() {
    const primary = filteredNodes();
    const primaryIds = new Set(primary.map((node) => node.id));
    const included = new Set(primaryIds);
    if (state.scope === "focus" && state.selectedId) {
      included.clear(); included.add(state.selectedId);
      graphEdges().forEach((edge) => {
        if (edge.source === state.selectedId) included.add(edge.target);
        if (edge.target === state.selectedId) included.add(edge.source);
      });
    } else if (state.tag) {
      // Class/module tags are local to their file. Include neighbors to expose boundaries.
      graphEdges().forEach((edge) => {
        if (primaryIds.has(edge.source)) included.add(edge.target);
        if (primaryIds.has(edge.target)) included.add(edge.source);
      });
    }
    let nodes = graphNodes().filter((node) => included.has(node.id));
    // Keep the selected function and tag members ahead of extra context when capped.
    nodes.sort((a, b) => Number(b.id === state.selectedId) - Number(a.id === state.selectedId)
      || Number(primaryIds.has(b.id)) - Number(primaryIds.has(a.id))
      || a.file.localeCompare(b.file) || a.line - b.line);
    const total = nodes.length; nodes = nodes.slice(0, MAX_GRAPH_NODES);
    return { nodes, total, primaryIds };
  }

  function renderGraph() {
    $("scope-all").classList.toggle("active", state.scope === "all");
    $("scope-focus").classList.toggle("active", state.scope === "focus");
    $("scope-all").setAttribute("aria-pressed", String(state.scope === "all"));
    $("scope-focus").setAttribute("aria-pressed", String(state.scope === "focus"));
    $("scope-focus").disabled = !currentNode();
    const svg = $("call-graph"); svg.replaceChildren();
    const { nodes, total, primaryIds } = graphSelection();
    const empty = !nodes.length;
    svg.toggleAttribute("hidden", empty); $("graph-empty").hidden = !empty;
    if (empty) {
      $("graph-empty").textContent = state.graph?.revision === 0 ? "正在读取代码结构…" : "没有可显示的函数。尝试选择其他项目或清除筛选。";
      $("graph-summary").textContent = "0 个函数"; return;
    }
    const fileMap = new Map();
    nodes.forEach((node) => {
      if (!fileMap.has(node.file)) fileMap.set(node.file, []);
      fileMap.get(node.file).push(node);
    });
    const width = Math.max(360, $("graph-viewport").clientWidth - 2);
    const columns = width >= 540 && fileMap.size > 1 ? 2 : 1;
    const gap = 26; const groupWidth = Math.floor((width - gap * (columns + 1)) / columns);
    const colHeights = Array(columns).fill(26); const positions = new Map();
    const backgroundLayer = svgElement("g");
    const edgeLayer = svgElement("g", { "aria-hidden": "true" });
    const nodeLayer = svgElement("g");
    const defs = svgElement("defs");
    [["arrow-call", "#72917a"], ["arrow-candidate", "#b99153"], ["arrow-selected", "#3e8b6e"]].forEach(([id, color]) => {
      const marker = svgElement("marker", { id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse" });
      marker.append(svgElement("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: color })); defs.append(marker);
    });
    svg.append(defs, backgroundLayer, edgeLayer, nodeLayer);
    [...fileMap.keys()].sort().forEach((file) => {
      const members = fileMap.get(file).sort((a, b) => a.line - b.line);
      const column = colHeights.indexOf(Math.min(...colHeights));
      const x = gap + column * (groupWidth + gap); const y = colHeights[column];
      const groupHeight = 49 + members.length * 73;
      const group = svgElement("g");
      group.append(svgElement("rect", { x, y, width: groupWidth, height: groupHeight, rx: 10, fill: "#f3f6ed", "fill-opacity": .88, stroke: "#dbe4d1", "stroke-width": 1 }));
      const fileTitle = svgElement("text", { x: x + 13, y: y + 22, class: "graph-file-label" }, truncate(file, Math.floor((groupWidth - 32) / 6.6)));
      fileTitle.append(svgElement("title", {}, file));
      group.append(fileTitle, svgElement("text", { x: x + 13, y: y + 37, class: "graph-file-count" }, `${members[0].language} · ${members.length} 个函数`));
      backgroundLayer.append(group);
      members.forEach((node, index) => positions.set(node.id, { x: x + 12, y: y + 49 + index * 73, w: groupWidth - 24, h: 58, node }));
      colHeights[column] += groupHeight + 25;
    });
    const visibleIds = new Set(nodes.map((node) => node.id));
    const edges = graphEdges().filter((edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target));
    const connected = new Set([state.selectedId]);
    edges.forEach((edge) => {
      if (edge.source === state.selectedId) connected.add(edge.target);
      if (edge.target === state.selectedId) connected.add(edge.source);
      const a = positions.get(edge.source); const b = positions.get(edge.target);
      const highlight = edge.source === state.selectedId || edge.target === state.selectedId;
      let d;
      if (edge.source === edge.target) {
        d = `M ${a.x + a.w} ${a.y + 17} C ${a.x + a.w + 20} ${a.y + 2}, ${a.x + a.w + 20} ${a.y + 57}, ${a.x + a.w} ${a.y + 42}`;
      } else if (Math.abs(a.x - b.x) < 2) {
        // Route same-column connections along the margin, outside intervening cards.
        const right = a.x + a.w; const bend = right + 10 + (Math.abs(a.y - b.y) % 4);
        d = `M ${right} ${a.y + a.h / 2} C ${bend} ${a.y + a.h / 2}, ${bend} ${b.y + b.h / 2}, ${b.x + b.w} ${b.y + b.h / 2}`;
      } else {
        const forward = b.x > a.x;
        const sx = forward ? a.x + a.w : a.x; const tx = forward ? b.x : b.x + b.w;
        const sy = a.y + a.h / 2; const ty = b.y + b.h / 2; const mid = (sx + tx) / 2;
        d = `M ${sx} ${sy} C ${mid} ${sy}, ${mid} ${ty}, ${tx} ${ty}`;
      }
      const classes = ["graph-edge", edge.status === "candidate" ? "candidate" : "", highlight ? "highlight" : ""].filter(Boolean).join(" ");
      edgeLayer.append(svgElement("path", { d, class: classes, "marker-end": `url(#${edge.status === "candidate" ? "arrow-candidate" : highlight ? "arrow-selected" : "arrow-call"})` }));
    });
    positions.forEach(({ x, y, w, h, node }) => {
      const external = state.tag && !primaryIds.has(node.id);
      const group = svgElement("g", { class: `graph-node${node.id === state.selectedId ? " selected" : ""}${external ? " external" : ""}`, tabindex: 0, role: "button", "aria-label": `${node.name}，${node.file}，第 ${node.line} 行` });
      group.append(svgElement("title", {}, `${node.qualified_name || node.name}\n${node.signature || ""}\n${node.file}:${node.line}${external ? "\n与标签内函数相连的外部函数" : ""}`));
      group.append(svgElement("rect", { x, y, width: w, height: h, rx: 7, class: "node-rect" }));
      group.append(svgElement("text", { x: x + 10, y: y + 21, class: "node-symbol" }, "ƒ"));
      group.append(svgElement("text", { x: x + 25, y: y + 21, class: "node-name" }, truncate(node.name, Math.floor((w - 40) / 6.6))));
      group.append(svgElement("text", { x: x + 11, y: y + 44, class: "node-detail" }, `${external ? "外部联系 · " : ""}L${node.line}`));
      group.addEventListener("click", () => selectNode(node.id));
      group.addEventListener("keydown", (event) => {
        if (event.target !== group) return;
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectNode(node.id); }
      });
      if (node.container) {
        const maxWidth = Math.max(55, w - (external ? 109 : 67));
        const label = truncate(`# ${node.container}`, Math.floor((maxWidth - 12) / 5));
        const tagWidth = Math.min(maxWidth, label.length * 5 + 12);
        const tag = svgElement("g", { class: `node-tag${isActiveTag(node) ? " active" : ""}`, tabindex: 0, role: "button", "aria-label": `筛选类或模块 ${node.container}`, "aria-pressed": String(isActiveTag(node)) });
        tag.append(svgElement("title", {}, `${node.file} · ${node.container}`));
        tag.append(svgElement("rect", { x: x + w - tagWidth - 9, y: y + 32, width: tagWidth, height: 17, rx: 4 }));
        tag.append(svgElement("text", { x: x + w - tagWidth - 3, y: y + 43.5 }, label));
        tag.addEventListener("click", (event) => { event.stopPropagation(); activateTag(node); });
        tag.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); event.stopPropagation(); activateTag(node); }
        });
        group.append(tag);
      }
      nodeLayer.append(group);
    });
    const height = Math.max(...colHeights, 220);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("width", width * state.zoom);
    svg.setAttribute("height", height * state.zoom);
    $("zoom-reset").textContent = `${Math.round(state.zoom * 100)}%`;
    const externalCount = nodes.filter((node) => state.tag && !primaryIds.has(node.id)).length;
    $("graph-summary").textContent = `${nodes.length} 个函数 · ${edges.length} 条关系${externalCount ? ` · ${externalCount} 个外部联系` : ""}${total > nodes.length ? `（显示前 ${MAX_GRAPH_NODES}/${total}，请搜索或聚焦）` : ""}`;
  }

  function selectNode(id, line) {
    if (!graphNodes().some((node) => node.id === id)) return;
    const changed = id !== state.selectedId;
    state.selectedId = id; state.form = null;
    renderTree(); renderGraph(); renderInspector();
    if (changed) { $("source-code").scrollTop = 0; $("source-code").scrollLeft = 0; }
    if (line) {
      setTab("source");
      const codeLine = [...$("source-code").children].find((row) => Number(row.dataset.line) === Number(line));
      if (codeLine) { codeLine.classList.add("highlight"); codeLine.scrollIntoView({ block: "nearest", inline: "nearest" }); }
    }
  }

  function renderInspectorTags() {
    const node = currentNode() || state.selectedSnapshot;
    if (!node) return;
    const tags = $("function-tags"); tags.replaceChildren();
    const language = element("button", "language-tag", node.language); language.type = "button";
    language.title = `筛选 ${node.language} 文件`;
    language.addEventListener("click", () => {
      state.language = state.language === node.language ? "" : node.language;
      $("language-filter").value = state.language; renderTree(); renderGraph();
    });
    tags.append(language, element("span", "kind-tag", node.kind === "method" ? "方法" : "函数"));
    if (node.container) tags.append(makeContainerTag(node));
  }

  function renderInspector() {
    const current = currentNode();
    if (current) state.selectedSnapshot = current;
    const node = current || state.selectedSnapshot;
    $("inspector-empty").hidden = Boolean(node); $("inspector").hidden = !node;
    if (!node) { state.form = null; return; }
    $("function-name").textContent = node.name;
    $("function-name").title = node.qualified_name || node.name;
    $("function-location").textContent = `${node.file} : ${node.line}–${node.end_line}`;
    renderInspectorTags();
    $("node-warning").hidden = Boolean(current);
    if (!current) $("node-warning").textContent = "该函数已从当前项目结构中消失，可能已删除、改名或解析失败。此处保留上次源码和未保存文字，不能提交；可导出未保存草稿。";
    $("source-lines").textContent = `L${node.line}–${node.end_line}`;
    const source = $("source-code"); const scrollTop = source.scrollTop; const scrollLeft = source.scrollLeft;
    source.replaceChildren();
    String(node.code || "").split("\n").forEach((text, index) => {
      const row = element("div", "code-line"); row.dataset.line = Number(node.line) + index;
      row.append(element("span", "line-number", Number(node.line) + index), element("span", "line-content", text.replace(/\r$/, "")));
      source.append(row);
    });
    source.scrollTop = scrollTop; source.scrollLeft = scrollLeft;
    renderRelations(node);
    $("extracted-doc").textContent = node.doc || "该函数未提取到说明注释。可以在下方添加结构化说明。";
    const draft = state.drafts.get(draftKey(node.id));
    if (draft) {
      state.form = draft;
      draft.conflict = !current || draft.codeHash !== current.code_hash || draft.expectedVersion !== annotationVersion(current);
    } else {
      const annotation = node.annotation || {};
      state.form = { id: node.id, projectRoot: projectKey(), values: Object.fromEntries(NOTE_FIELDS.map((key) => [key, annotation[key] || (key === "origin" ? "manual" : "")])), codeHash: node.code_hash, expectedVersion: annotationVersion(node), dirty: false, conflict: false };
    }
    NOTE_FIELDS.forEach((field) => {
      // Preserve the caret when a background rescan keeps a dirty field unchanged.
      if ($(`note-${field}`).value !== state.form.values[field]) $(`note-${field}`).value = state.form.values[field];
    });
    renderAnnotationStatus(node); setTab(state.tab);
  }

  function renderRelations(node) {
    [["outgoing", graphEdges().filter((edge) => edge.source === node.id), "target"], ["incoming", graphEdges().filter((edge) => edge.target === node.id), "source"]].forEach(([name, edges, targetField]) => {
      const list = $(`${name}-list`); list.replaceChildren(); $(`${name}-count`).textContent = edges.length;
      edges.forEach((edge) => {
        const target = graphNodes().find((entry) => entry.id === edge[targetField]); if (!target) return;
        const button = element("button", `relation-row${edge.status === "candidate" ? " candidate" : ""}`); button.type = "button";
        const strong = element("strong", "", target.qualified_name || target.name);
        strong.append(element("span", "", edge.status === "candidate" ? "候选" : "静态已解析"));
        button.append(strong, element("small", "", `${target.file} · 调用位置 L${edge.line}${edge.evidence ? ` · ${edge.evidence}` : ""}`));
        button.title = edge.evidence || "点击阅读关联函数";
        button.addEventListener("click", () => selectNode(target.id, name === "incoming" ? edge.line : undefined));
        list.append(button);
      });
      if (!edges.length) list.append(element("p", "relation-empty", name === "outgoing" ? "未发现可关联到项目函数的调用。" : "项目内未发现对它的静态调用；不代表运行时不会使用。"));
    });
    const unresolved = (state.graph?.unresolved || []).filter((call) => call.source === node.id);
    $("unresolved-count").textContent = unresolved.length; $("unresolved-list").replaceChildren();
    unresolved.forEach((call) => {
      const row = element("div", "unresolved-row");
      row.append(element("strong", "", `${call.name} · L${call.line}`), element("small", "", call.reason || "无法确定调用目标"));
      $("unresolved-list").append(row);
    });
    if (!unresolved.length) $("unresolved-list").append(element("p", "relation-empty", "当前解析结果没有记录未解析调用。"));
  }

  function renderAnnotationStatus(node) {
    const annotation = node.annotation; const form = state.form;
    const statusLabels = { approved: "人工已确认", draft: "待审核草稿", stale: "代码变化 · 待复核" };
    $("annotation-status").className = `annotation-status ${annotation?.status || ""}`;
    $("annotation-status").textContent = form?.dirty ? "有未保存修改" : (statusLabels[annotation?.status] || "未添加");
    $("dirty-dot").hidden = !form?.dirty;
    $("annotation-conflict").hidden = !form?.conflict;
    $("conflict-text").textContent = !currentNode() ? "函数暂不可用。未保存文字仍保留在本页，请导出备份。"
      : "代码或已保存说明发生变化。你的文字已保留；请先核对当前源码，再选择保留草稿或重新载入。";
    $("rebase-draft").disabled = !currentNode();
    $("reload-annotation").disabled = !currentNode();
    $("save-draft").disabled = state.saving || !currentNode() || Boolean(form?.conflict);
    $("approve-annotation").disabled = state.saving || !currentNode() || Boolean(form?.conflict);
    // A submission is a fixed snapshot: disallow edits until its result is known.
    NOTE_FIELDS.forEach((field) => { $(`note-${field}`).disabled = state.saving; });
    const origin = annotation?.origin === "ai" ? "AI 提供" : "人工添加 / 编辑";
    $("annotation-meta").textContent = annotation
      ? `已保存 v${annotation.version} · ${origin}${annotation.status === "stale" ? "。代码版本已变化，旧确认需重新核查。" : "。历史版本保留。"}`
      : "说明保存在阅读器本地数据库中，源文件不会被修改。";
  }

  function captureDraft() {
    if (!state.form) return;
    NOTE_FIELDS.forEach((field) => { state.form.values[field] = $(`note-${field}`).value; });
    state.form.dirty = true;
    state.drafts.set(`${state.form.projectRoot}\u0000${state.form.id}`, state.form);
    renderAnnotationStatus(currentNode() || state.selectedSnapshot); updateStats();
  }

  async function saveAnnotation(status) {
    if (!state.form || !currentNode() || state.form.conflict || state.saving || state.switching) return;
    captureDraft();
    const form = state.form; const key = `${form.projectRoot}\u0000${form.id}`;
    state.saving = true; renderAnnotationStatus(currentNode());
    try {
      const result = await post("/api/annotations", {
        id: form.id, project_root: form.projectRoot, code_hash: form.codeHash, expected_version: form.expectedVersion,
        ...form.values, status,
      });
      state.drafts.delete(key);
      const node = graphNodes().find((entry) => entry.id === form.id);
      if (node) node.annotation = result.annotation;
      if (state.selectedId === form.id) { state.form = null; renderInspector(); }
      notify(status === "approved" ? "已人工确认此代码版本；后续代码变化将提示复核。" : "草稿已保存。源文件未修改。");
      await fetchGraph();
    } catch (error) {
      if (error.status === 409) {
        form.conflict = true;
        try { await fetchGraph(); } catch (_) { /* Keep the user's draft even when refresh fails. */ }
        if (state.selectedId === form.id) renderAnnotationStatus(currentNode() || state.selectedSnapshot);
        notify(`保存发生版本冲突，草稿已保留。${error.message}`, true);
      } else notify(`说明保存失败：${error.message}`, true);
    } finally {
      state.saving = false;
      if (currentNode() || state.selectedSnapshot) renderAnnotationStatus(currentNode() || state.selectedSnapshot);
      updateStats();
    }
  }

  function setTab(tab) {
    state.tab = tab;
    ["source", "annotation"].forEach((name) => {
      $(`tab-${name}`).classList.toggle("active", name === tab);
      $(`tab-${name}`).setAttribute("aria-selected", String(name === tab));
      $(`tab-${name}`).tabIndex = name === tab ? 0 : -1;
      $(`${name}-panel`).hidden = name !== tab;
    });
  }

  function renderDiagnostics() {
    const diagnostics = [...(state.graph?.diagnostics || [])];
    if (state.graph?.error) diagnostics.unshift({ level: "error", message: state.graph.error });
    $("diagnostic-count").textContent = diagnostics.length;
    $("diagnostics-list").replaceChildren();
    diagnostics.forEach((diagnostic) => {
      const item = element("div", "diagnostic-item");
      item.append(element("strong", "", diagnostic.file || "项目"), element("p", "", `${diagnostic.level === "error" ? "错误：" : "提示："}${diagnostic.message}`));
      $("diagnostics-list").append(item);
    });
    if (!diagnostics.length) $("diagnostics-list").append(element("p", "helper", "当前没有解析提示。图仅展示静态提取结果，动态调用、外部依赖和运行时分派可能仍无法确定。"));
  }

  async function openProject(path) {
    if (state.saving) { notify("正在保存说明，请保存完成后再切换项目。", true); return; }
    if (!path.trim()) { notify("请先填写一个本地项目文件夹路径。", true); $("project-path").focus(); return; }
    state.switching = true; state.fetchSequence += 1;
    $("open-project").disabled = true; $("pick-directory").disabled = true;
    try {
      const graph = await post("/api/project", { path: path.trim() });
      applyGraph(graph, true);
      const retained = [...state.drafts.keys()].some((key) => !key.startsWith(`${projectKey()}\u0000`));
      notify(`正在阅读 ${graph.project?.name || "所选项目"}。文件保存后会自动更新关系图。${retained ? "其他项目的未保存草稿仍保留在本页，切回原项目即可继续。" : ""}`);
    } catch (error) { notify(`无法打开项目：${error.message}`, true); }
    finally { state.switching = false; $("open-project").disabled = false; $("pick-directory").disabled = false; }
  }

  async function pickDirectory() {
    $("pick-directory").disabled = true;
    try {
      const result = await post("/api/pick-directory", {});
      if (!result.cancelled && result.path) { $("project-path").value = result.path; await openProject(result.path); }
    } catch (error) { notify(`目录选择器暂不可用，可直接输入路径。${error.message}`, true); $("project-path").focus(); }
    finally { $("pick-directory").disabled = false; }
  }

  async function exportAnnotations() {
    const requestedRoot = projectKey();
    try {
      const data = await api("/api/annotations/export");
      if (data.project?.root !== requestedRoot) throw new Error("当前服务的项目已被其他页面切换，请刷新项目视图后重新导出。");
      // Keep unsaved drafts distinct from server-approved records, including removed nodes.
      const unsaved = [...state.drafts.entries()].filter(([key]) => key.startsWith(`${requestedRoot}\u0000`))
        .map(([, draft]) => ({ id: draft.id, ...draft.values, code_hash: draft.codeHash, expected_version: draft.expectedVersion, status: "draft", unsaved: true }));
      if (unsaved.length) data.unsaved_drafts = unsaved;
      const blob = new Blob([JSON.stringify(data, null, 2) + "\n"], { type: "application/json;charset=utf-8" });
      const url = URL.createObjectURL(blob); const link = element("a");
      link.href = url; link.download = `vibereading-notes-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      notify(`说明已导出${unsaved.length ? `，另含 ${unsaved.length} 条未保存草稿（unsaved_drafts）` : ""}。`);
    } catch (error) { notify(`导出失败：${error.message}`, true); }
  }

  async function importAnnotations(file) {
    if (!file) return;
    const importProjectRoot = projectKey();
    if (file.size > 2 * 1024 * 1024) { notify("导入文件超过 2 MiB，请拆分后再导入。", true); return; }
    try {
      const data = JSON.parse(await file.text());
      const items = Array.isArray(data) ? data : data.items;
      if (!Array.isArray(items)) throw new Error("JSON 应为条目数组，或包含 items 数组。条目至少需要当前函数的 id。");
      const result = await post("/api/annotations/import", { items, project_root: importProjectRoot });
      const count = (value) => Array.isArray(value) ? value.length : (value || 0);
      notify(`导入 ${count(result.imported)} 条 AI 草稿，跳过 ${count(result.skipped)} 条。导入不会自动获得人工确认。`);
      await fetchGraph();
    } catch (error) { notify(`导入失败：${error.message}`, true); }
    finally { $("import-file").value = ""; }
  }

  async function poll() {
    if (state.polling || state.switching || state.saving) return;
    state.polling = true;
    try {
      const status = await api("/api/status"); updateWatch(status);
      if (!state.graph || status.revision !== state.graph.revision || state.lastConnectionError) await fetchGraph();
      state.lastConnectionError = false;
    } catch (_) { state.lastConnectionError = true; updateWatch(null, true); }
    finally { state.polling = false; }
  }

  // Register UI actions after parsing. Polling never executes code from the project.
  $("dismiss-notice").addEventListener("click", () => { $("notice").hidden = true; });
  $("project-form").addEventListener("submit", (event) => { event.preventDefault(); openProject($("project-path").value); });
  $("pick-directory").addEventListener("click", pickDirectory);
  $("refresh").addEventListener("click", async () => {
    try { await fetchGraph(); notify("视图已刷新；磁盘中的代码变化由文件监听自动读取。"); }
    catch (error) { notify(error.message, true); }
  });
  $("search").addEventListener("input", (event) => { state.search = event.target.value; renderTree(); renderGraph(); });
  $("language-filter").addEventListener("change", (event) => { state.language = event.target.value; renderTree(); renderGraph(); });
  $("clear-tag").addEventListener("click", () => { state.tag = null; renderTree(); renderGraph(); renderInspectorTags(); });
  $("scope-all").addEventListener("click", () => { state.scope = "all"; renderGraph(); });
  $("scope-focus").addEventListener("click", () => { state.scope = "focus"; renderGraph(); });
  $("zoom-out").addEventListener("click", () => { state.zoom = Math.max(.6, Math.round((state.zoom - .1) * 10) / 10); renderGraph(); });
  $("zoom-in").addEventListener("click", () => { state.zoom = Math.min(2, Math.round((state.zoom + .1) * 10) / 10); renderGraph(); });
  $("zoom-reset").addEventListener("click", () => { state.zoom = 1; renderGraph(); });
  $("tab-source").addEventListener("click", () => setTab("source"));
  $("tab-annotation").addEventListener("click", () => setTab("annotation"));
  ["tab-source", "tab-annotation"].forEach((id) => $(id).addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault(); setTab(state.tab === "source" ? "annotation" : "source"); $(`tab-${state.tab}`).focus();
    }
  }));
  $("annotation-form").addEventListener("input", captureDraft);
  $("note-origin").addEventListener("change", captureDraft);
  $("annotation-form").addEventListener("submit", (event) => { event.preventDefault(); saveAnnotation("draft"); });
  $("approve-annotation").addEventListener("click", () => saveAnnotation("approved"));
  $("rebase-draft").addEventListener("click", () => {
    const node = currentNode(); if (!node || !state.form) return;
    state.form.codeHash = node.code_hash; state.form.expectedVersion = annotationVersion(node);
    state.form.conflict = false; captureDraft();
    notify("已将保留文字关联到当前代码版本，尚未保存或确认。请核对说明后提交。");
  });
  $("reload-annotation").addEventListener("click", () => {
    if (!currentNode()) return;
    if (state.form?.dirty && !window.confirm("重新载入会放弃当前函数尚未保存的文字。确定重新载入吗？")) return;
    state.drafts.delete(draftKey(state.selectedId)); state.form = null; renderInspector(); updateStats();
  });
  $("import-annotations").addEventListener("click", () => $("import-file").click());
  $("import-file").addEventListener("change", (event) => importAnnotations(event.target.files[0]));
  $("export-annotations").addEventListener("click", exportAnnotations);
  function setDiagnostics(open) { $("diagnostics-panel").hidden = !open; $("diagnostics-toggle").setAttribute("aria-expanded", String(open)); }
  $("diagnostics-toggle").addEventListener("click", () => setDiagnostics($("diagnostics-panel").hidden));
  $("close-diagnostics").addEventListener("click", () => setDiagnostics(false));
  window.addEventListener("beforeunload", (event) => {
    if (state.drafts.size) { event.preventDefault(); event.returnValue = ""; }
  });
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(renderGraph, 180); });
  fetchGraph().catch((error) => { notify(`无法连接本地服务：${error.message}`, true); updateWatch(null, true); });
  setInterval(poll, 1000);
})();
