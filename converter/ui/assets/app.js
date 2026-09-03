/* Offline flow viewer.
   Plain ES2018, no build step, no network, no external libraries: the file is
   opened straight from disk on a machine that may have no internet at all.
   Everything it draws comes from the embedded intermediate representation. */
(function () {
  "use strict";

  /* The payload is set either by the single-file offline viewer, which embeds
     it, or by the web page once a conversion has come back from the server. */
  var DATA = window.__ACCESS_IR__ || null;
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var state = {
    view: "dashboard",
    objectId: null,
    diagramId: null,
    tab: "overview",
    query: "",
    kinds: {},
    onlyProblems: false,
    mapRoot: null,
    mapDepth: 2,
    zoom: 1, panX: 0, panY: 0,
    selectedNode: null
  };

  /** The three aspects, in the order they are always shown. */
  var ASPECT_KEYS = ["structure", "data_logic", "application_logic"];

  /* ---------------------------------------------------------------- i18n */

  var I18N = window.__ACCESS_I18N__;
  state.lang = I18N.pickInitialLanguage();

  function cat() { return I18N.catalogues[state.lang] || I18N.catalogues.en; }

  /** Look up "ui.colName" style paths, falling back to English then the key.
      A missing key shows as the key itself rather than as an empty cell, so a
      gap in a catalogue is visible instead of silently blank. */
  function t(path, args) {
    var value = dig(cat(), path);
    if (value === undefined) { value = dig(I18N.catalogues.en, path); }
    if (value === undefined) { return path; }
    if (!args) { return value; }
    return value.replace(/\{(\w+)\}/g, function (whole, key) {
      return args[key] === undefined ? whole : String(args[key]);
    });
  }

  function dig(root, path) {
    var parts = path.split(".");
    var node = root;
    for (var i = 0; i < parts.length; i++) {
      if (node === undefined || node === null) { return undefined; }
      node = node[parts[i]];
    }
    return node;
  }

  function kindLabel(kind) { return t("kind." + kind) === "kind." + kind ? kind : t("kind." + kind); }
  function statusLabel(status) {
    return t("status." + status) === "status." + status ? status : t("status." + status);
  }
  function aspectLabel(aspect) { return t("aspect." + aspect); }

  /** Flow-chart wording arrives as a key so one IR can serve every locale. */
  function nodeText(node) {
    if (node.label) { return node.label; }
    if (node.text_key) { return t("flow." + node.text_key, node.text_args); }
    return "";
  }
  function edgeText(edge) {
    if (edge.label) { return edge.label; }
    if (edge.label_key) { return t("flow." + edge.label_key); }
    return "";
  }

  var NODE_STYLE = {
    start:      { fill: "#e0f2e7", stroke: "#14713f", text: "#0d4527", shape: "pill" },
    end:        { fill: "#eceff2", stroke: "#55606b", text: "#26303a", shape: "pill" },
    decision:   { fill: "#fdefda", stroke: "#9a5b00", text: "#5a3600", shape: "diamond" },
    loop:       { fill: "#e7e9ff", stroke: "#33409e", text: "#212a72", shape: "hex" },
    process:    { fill: "#ffffff", stroke: "#6b7681", text: "#14181d", shape: "rect" },
    declaration:{ fill: "#f7f9fb", stroke: "#aeb7c0", text: "#4a545e", shape: "rect" },
    data:       { fill: "#e3f0ff", stroke: "#14458f", text: "#0d2f66", shape: "para" },
    ui:         { fill: "#f3e8fd", stroke: "#6b21a8", text: "#4a1573", shape: "rect2" },
    io:         { fill: "#fff3d6", stroke: "#8a5300", text: "#5a3600", shape: "para" },
    error:      { fill: "#fce8e6", stroke: "#a51f14", text: "#6d140d", shape: "rect" },
    exit:       { fill: "#eceff2", stroke: "#a51f14", text: "#6d140d", shape: "pill" },
    goto:       { fill: "#eceff2", stroke: "#8a5300", text: "#5a3600", shape: "rect" },
    label:      { fill: "#eef1f4", stroke: "#55606b", text: "#26303a", shape: "tag" },
    merge:      { fill: "#9aa5b1", stroke: "#6b7681", text: "#14181d", shape: "dot" },
    table:      { fill: "#e3f0ff", stroke: "#14458f", text: "#0d2f66", shape: "rect" },
    query:      { fill: "#e0f2e7", stroke: "#14713f", text: "#0d4527", shape: "para" },
    form:       { fill: "#f3e8fd", stroke: "#6b21a8", text: "#4a1573", shape: "rect2" },
    report:     { fill: "#fdeae8", stroke: "#a51f14", text: "#6d140d", shape: "rect2" },
    macro:      { fill: "#fff3d6", stroke: "#8a5300", text: "#5a3600", shape: "hex" },
    module:     { fill: "#e7e9ff", stroke: "#33409e", text: "#212a72", shape: "rect" },
    procedure:  { fill: "#ffffff", stroke: "#6b7681", text: "#14181d", shape: "rect" }
  };


  /** The wording for a reason code, in the reader's language.

      Reason codes stay in English everywhere - they are identifiers shared by
      the JSON, the documentation and bug reports - while the sentence beside
      each one is translated. A code with no catalogue entry falls back to the
      English note that travels in the data, so a newly added code is never
      shown as a blank cell. */
  function reasonText(code, fallback) {
    var text = t("reason." + code);
    return text === "reason." + code ? (fallback || "") : text;
  }

  function featureLabel(name) {
    var text = t("feature." + name);
    return text === "feature." + name ? name : text;
  }

  function esc(text) {
    return String(text === null || text === undefined ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function pct(value) { return (Math.round(value * 100) / 100).toFixed(2) + "%"; }

  /* ---------------------------------------------------------- text metrics */

  var measureCtx = document.createElement("canvas").getContext("2d");
  function textWidth(text, font) {
    measureCtx.font = font || "600 15px " +
      getComputedStyle(document.body).getPropertyValue("--font");
    return measureCtx.measureText(text).width;
  }
  function wrap(text, maxWidth, maxLines) {
    var words = String(text || "").split(/(\s+)/).filter(function (w) { return w !== ""; });
    var lines = [], current = "";
    for (var i = 0; i < words.length; i++) {
      var candidate = current + words[i];
      if (textWidth(candidate) > maxWidth && current !== "") {
        lines.push(current.trim());
        current = words[i].replace(/^\s+/, "");
        if (lines.length === maxLines - 1) { break; }
      } else {
        current = candidate;
      }
    }
    var rest = current + words.slice(lines.join("").length ? 0 : 0).join("");
    if (lines.length < maxLines) { lines.push(current.trim()); }
    // Hard-truncate a final line that is still too wide (long identifiers).
    var last = lines[lines.length - 1] || "";
    while (textWidth(last) > maxWidth && last.length > 1) { last = last.slice(0, -1); }
    if (last !== lines[lines.length - 1]) { lines[lines.length - 1] = last + "…"; }
    return lines.filter(function (l) { return l !== ""; });
  }

  /* --------------------------------------------------------------- layout */

  function layoutGraph(graph, options) {
    options = options || {};
    var horizontal = !!options.horizontal;
    var nodes = graph.nodes.map(function (n) { return Object.assign({}, n); });
    var index = {};
    nodes.forEach(function (n, i) { index[n.id] = i; });
    var edges = graph.edges.filter(function (e) {
      return index[e.from] !== undefined && index[e.to] !== undefined;
    }).map(function (e) { return Object.assign({}, e); });

    /* Size every node from its wrapped label. */
    nodes.forEach(function (n) {
      var style = NODE_STYLE[n.kind] || NODE_STYLE.process;
      if (style.shape === "dot") { n.w = 16; n.h = 16; n.lines = []; return; }
      var maxWidth = style.shape === "diamond" ? 200 : 230;
      n.lines = wrap(nodeText(n), maxWidth, 3);
      var widest = 0;
      n.lines.forEach(function (l) { widest = Math.max(widest, textWidth(l)); });
      n.w = Math.max(96, Math.min(300, widest + (style.shape === "diamond" ? 74 : 38)));
      n.h = Math.max(48, n.lines.length * 22 + (style.shape === "diamond" ? 34 : 24));
    });

    /* Break cycles with a depth-first search so layering always terminates. */
    var outgoing = {};
    nodes.forEach(function (n) { outgoing[n.id] = []; });
    edges.forEach(function (e) { if (e.from !== e.to) { outgoing[e.from].push(e); } });
    var colour = {}, back = {};
    function visit(id) {
      colour[id] = 1;
      outgoing[id].forEach(function (e) {
        if (colour[e.to] === 1) { back[e.from + "->" + e.to] = true; }
        else if (!colour[e.to]) { visit(e.to); }
      });
      colour[id] = 2;
    }
    var roots = nodes.filter(function (n) {
      return !edges.some(function (e) { return e.to === n.id && e.from !== e.to; });
    });
    (roots.length ? roots : nodes.slice(0, 1)).forEach(function (n) { if (!colour[n.id]) { visit(n.id); } });
    nodes.forEach(function (n) { if (!colour[n.id]) { visit(n.id); } });

    var forward = edges.filter(function (e) {
      return e.from !== e.to && !back[e.from + "->" + e.to];
    });

    /* Longest-path layering over the acyclic subgraph. */
    var layer = {};
    nodes.forEach(function (n) { layer[n.id] = 0; });
    var incomingCount = {};
    nodes.forEach(function (n) { incomingCount[n.id] = 0; });
    forward.forEach(function (e) { incomingCount[e.to] += 1; });
    var queue = nodes.filter(function (n) { return incomingCount[n.id] === 0; })
      .map(function (n) { return n.id; });
    var processed = 0;
    while (queue.length) {
      var id = queue.shift();
      processed += 1;
      forward.forEach(function (e) {
        if (e.from !== id) { return; }
        layer[e.to] = Math.max(layer[e.to], layer[id] + 1);
        incomingCount[e.to] -= 1;
        if (incomingCount[e.to] === 0) { queue.push(e.to); }
      });
      if (processed > nodes.length * 4) { break; }
    }

    /* Order inside each layer with a few barycentre sweeps. */
    var layers = [];
    nodes.forEach(function (n) {
      var l = layer[n.id];
      (layers[l] = layers[l] || []).push(n);
    });
    for (var i = 0; i < layers.length; i++) { layers[i] = layers[i] || []; }
    var position = {};
    layers.forEach(function (row) { row.forEach(function (n, i) { position[n.id] = i; }); });
    for (var sweep = 0; sweep < 4; sweep++) {
      for (var li = 1; li < layers.length; li++) {
        layers[li].sort(function (a, b) { return bary(a, "in") - bary(b, "in"); });
        layers[li].forEach(function (n, i) { position[n.id] = i; });
      }
      for (var lj = layers.length - 2; lj >= 0; lj--) {
        layers[lj].sort(function (a, b) { return bary(a, "out") - bary(b, "out"); });
        layers[lj].forEach(function (n, i) { position[n.id] = i; });
      }
    }
    function bary(node, direction) {
      var related = forward.filter(function (e) {
        return direction === "in" ? e.to === node.id : e.from === node.id;
      }).map(function (e) { return position[direction === "in" ? e.from : e.to]; })
        .filter(function (v) { return v !== undefined; });
      if (!related.length) { return position[node.id]; }
      return related.reduce(function (a, b) { return a + b; }, 0) / related.length;
    }

    /* Coordinates. */
    var gapMain = 74, gapCross = 34;
    var offset = 0;
    var depthSizes = layers.map(function (row) {
      return row.reduce(function (max, n) {
        return Math.max(max, horizontal ? n.w : n.h);
      }, 0);
    });
    layers.forEach(function (row, li) {
      var crossTotal = row.reduce(function (sum, n) {
        return sum + (horizontal ? n.h : n.w) + gapCross;
      }, -gapCross);
      var cursor = -crossTotal / 2;
      row.forEach(function (n) {
        var cross = horizontal ? n.h : n.w;
        if (horizontal) { n.y = cursor + cross / 2; n.x = offset + n.w / 2; }
        else { n.x = cursor + cross / 2; n.y = offset + n.h / 2; }
        cursor += cross + gapCross;
      });
      offset += depthSizes[li] + gapMain;
    });

    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    nodes.forEach(function (n) {
      minX = Math.min(minX, n.x - n.w / 2); maxX = Math.max(maxX, n.x + n.w / 2);
      minY = Math.min(minY, n.y - n.h / 2); maxY = Math.max(maxY, n.y + n.h / 2);
    });
    var padding = 40;
    nodes.forEach(function (n) { n.x += padding - minX; n.y += padding - minY; });
    return {
      nodes: nodes,
      edges: edges,
      back: back,
      width: (maxX - minX) + padding * 2,
      height: (maxY - minY) + padding * 2
    };
  }

  /* --------------------------------------------------------------- drawing */

  function shapePath(node) {
    var style = NODE_STYLE[node.kind] || NODE_STYLE.process;
    var x = node.x - node.w / 2, y = node.y - node.h / 2, w = node.w, h = node.h;
    switch (style.shape) {
      case "pill":
        return "M" + (x + h / 2) + "," + y + " H" + (x + w - h / 2) +
               " a" + (h / 2) + "," + (h / 2) + " 0 0 1 0," + h +
               " H" + (x + h / 2) + " a" + (h / 2) + "," + (h / 2) + " 0 0 1 0," + -h + " Z";
      case "diamond":
        return "M" + node.x + "," + y + " L" + (x + w) + "," + node.y +
               " L" + node.x + "," + (y + h) + " L" + x + "," + node.y + " Z";
      case "hex":
        return "M" + (x + 18) + "," + y + " H" + (x + w - 18) + " L" + (x + w) + "," + node.y +
               " L" + (x + w - 18) + "," + (y + h) + " H" + (x + 18) + " L" + x + "," + node.y + " Z";
      case "para":
        return "M" + (x + 18) + "," + y + " H" + (x + w) + " L" + (x + w - 18) + "," + (y + h) +
               " H" + x + " Z";
      case "dot":
        return "M" + (node.x - 8) + "," + node.y + " a8,8 0 1 0 16,0 a8,8 0 1 0 -16,0 Z";
      case "tag":
        return "M" + x + "," + y + " H" + (x + w - 14) + " L" + (x + w) + "," + node.y +
               " L" + (x + w - 14) + "," + (y + h) + " H" + x + " Z";
      default:
        return "M" + (x + 8) + "," + y + " H" + (x + w - 8) + " a8,8 0 0 1 8,8 V" + (y + h - 8) +
               " a8,8 0 0 1 -8,8 H" + (x + 8) + " a8,8 0 0 1 -8,-8 V" + (y + 8) +
               " a8,8 0 0 1 8,-8 Z";
    }
  }

  function edgePath(from, to, horizontal, isBack) {
    var x1 = from.x, y1 = from.y, x2 = to.x, y2 = to.y;
    if (horizontal) {
      x1 = from.x + from.w / 2; x2 = to.x - to.w / 2;
      if (isBack || x2 < x1) {
        var lift = Math.max(from.h, to.h) / 2 + 26;
        return "M" + from.x + "," + (from.y - from.h / 2) +
               " C" + from.x + "," + (from.y - from.h / 2 - lift) +
               " " + to.x + "," + (to.y - to.h / 2 - lift) +
               " " + to.x + "," + (to.y - to.h / 2);
      }
      var mid = (x1 + x2) / 2;
      return "M" + x1 + "," + y1 + " C" + mid + "," + y1 + " " + mid + "," + y2 + " " + x2 + "," + y2;
    }
    y1 = from.y + from.h / 2; y2 = to.y - to.h / 2;
    if (isBack || y2 < y1) {
      var side = Math.max(from.w, to.w) / 2 + 30;
      return "M" + (from.x + from.w / 2) + "," + from.y +
             " C" + (from.x + from.w / 2 + side) + "," + from.y +
             " " + (to.x + to.w / 2 + side) + "," + to.y +
             " " + (to.x + to.w / 2) + "," + to.y;
    }
    var midY = (y1 + y2) / 2;
    return "M" + x1 + "," + y1 + " C" + x1 + "," + midY + " " + x2 + "," + midY + " " + x2 + "," + y2;
  }

  function renderDiagram(container, graph, options) {
    options = options || {};
    var laid = layoutGraph(graph, options);
    var svgParts = [];
    svgParts.push('<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" ' +
      'markerWidth="7" markerHeight="7" orient="auto-start-reverse">' +
      '<path d="M0,0 L10,5 L0,10 z" fill="#6b7681"/></marker>' +
      '<marker id="arrow-error" viewBox="0 0 10 10" refX="9" refY="5" ' +
      'markerWidth="7" markerHeight="7" orient="auto-start-reverse">' +
      '<path d="M0,0 L10,5 L0,10 z" fill="#a51f14"/></marker></defs>');
    var byId = {};
    laid.nodes.forEach(function (n) { byId[n.id] = n; });

    svgParts.push('<g class="edges">');
    laid.edges.forEach(function (e) {
      var from = byId[e.from], to = byId[e.to];
      if (!from || !to) { return; }
      var isBack = !!laid.back[e.from + "->" + e.to];
      var cls = "edgepath" + (e.kind === "error" ? " error" : "");
      var marker = e.kind === "error" ? "url(#arrow-error)" : "url(#arrow)";
      svgParts.push('<path class="' + cls + '" d="' + edgePath(from, to, options.horizontal, isBack) +
        '" marker-end="' + marker + '" data-from="' + esc(e.from) + '" data-to="' + esc(e.to) + '"/>');
      var edgeLabel = edgeText(e);
      if (edgeLabel) {
        var lx = (from.x + to.x) / 2, ly = (from.y + to.y) / 2;
        svgParts.push('<text class="edgelabel" x="' + lx + '" y="' + ly +
          '" text-anchor="middle" paint-order="stroke" stroke="#fbfcfd" stroke-width="5">' +
          esc(edgeLabel) + '</text>');
      }
    });
    svgParts.push("</g><g class=\"nodes\">");
    laid.nodes.forEach(function (n) {
      var style = NODE_STYLE[n.kind] || NODE_STYLE.process;
      var statusStroke = n.status === "partial" ? "#9a5b00"
        : (n.status === "failed" || n.status === "unsupported") ? "#a51f14" : null;
      svgParts.push('<g class="nodebox" data-id="' + esc(n.id) + '" tabindex="0">');
      svgParts.push('<path class="shape" d="' + shapePath(n) + '" fill="' + style.fill +
        '" stroke="' + (statusStroke || style.stroke) + '"/>');
      if (style.shape === "rect2") {
        svgParts.push('<path d="M' + (n.x - n.w / 2 + 7) + ',' + (n.y - n.h / 2 + 5) +
          ' V' + (n.y + n.h / 2 - 5) + '" stroke="' + style.stroke + '" stroke-width="3"/>');
      }
      var startY = n.y - ((n.lines.length - 1) * 11);
      n.lines.forEach(function (line, i) {
        svgParts.push('<text class="nodelabel" x="' + n.x + '" y="' + (startY + i * 22 + 5) +
          '" text-anchor="middle" fill="' + style.text + '">' + esc(line) + "</text>");
      });
      svgParts.push("</g>");
    });
    svgParts.push("</g>");

    container.innerHTML =
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + laid.width + " " + laid.height +
      '" preserveAspectRatio="xMidYMin meet"><g class="viewport">' + svgParts.join("") + "</g></svg>";
    return laid;
  }

  /* ------------------------------------------------------------ pan & zoom */

  function attachStage(stage, laid, onSelect) {
    var svg = $("svg", stage);
    var viewport = $(".viewport", stage);
    var view = { x: 0, y: 0, k: 1 };
    function apply() {
      viewport.setAttribute("transform",
        "translate(" + view.x + "," + view.y + ") scale(" + view.k + ")");
    }
    function fit() {
      var box = stage.getBoundingClientRect();
      /* Fit the width and let the reader pan vertically. Shrinking a tall
         flow chart to fit the height makes the labels unreadable, which
         defeats the purpose of drawing it. */
      var k = Math.min(box.width / laid.width, 1.0) * 0.94;
      view.k = Math.max(0.55, k);
      if (laid.width * view.k > box.width) { view.k = box.width / laid.width; }
      view.x = (box.width - laid.width * view.k) / 2;
      view.y = 24;
      svg.setAttribute("viewBox", "0 0 " + box.width + " " + box.height);
      apply();
    }
    stage.__fit = fit;
    stage.__zoom = function (factor) {
      var box = stage.getBoundingClientRect();
      var cx = box.width / 2, cy = box.height / 2;
      view.x = cx - (cx - view.x) * factor;
      view.y = cy - (cy - view.y) * factor;
      view.k *= factor;
      apply();
    };
    var dragging = false, lastX = 0, lastY = 0;
    stage.addEventListener("pointerdown", function (event) {
      dragging = true; lastX = event.clientX; lastY = event.clientY;
      stage.classList.add("dragging");
      stage.setPointerCapture(event.pointerId);
    });
    stage.addEventListener("pointermove", function (event) {
      if (!dragging) { return; }
      view.x += event.clientX - lastX; view.y += event.clientY - lastY;
      lastX = event.clientX; lastY = event.clientY;
      apply();
    });
    ["pointerup", "pointercancel"].forEach(function (type) {
      stage.addEventListener(type, function (event) {
        dragging = false; stage.classList.remove("dragging");
        try { stage.releasePointerCapture(event.pointerId); } catch (e) { /* already released */ }
      });
    });
    stage.addEventListener("wheel", function (event) {
      event.preventDefault();
      var factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
      var box = stage.getBoundingClientRect();
      var px = event.clientX - box.left, py = event.clientY - box.top;
      view.x = px - (px - view.x) * factor;
      view.y = py - (py - view.y) * factor;
      view.k *= factor;
      apply();
    }, { passive: false });

    stage.addEventListener("click", function (event) {
      var box = event.target.closest(".nodebox");
      if (!box) { return; }
      Array.prototype.forEach.call(stage.querySelectorAll(".nodebox"), function (el) {
        el.classList.remove("selected");
      });
      box.classList.add("selected");
      if (onSelect) { onSelect(box.getAttribute("data-id")); }
    });
    window.requestAnimationFrame(fit);
    window.addEventListener("resize", fit);
    return fit;
  }

  /* --------------------------------------------------------------- sidebar */

  function objectsFiltered() {
    var query = state.query.trim().toLowerCase();
    return DATA.flow.objects.filter(function (o) {
      if (state.kinds[o.kind] === false) { return false; }
      if (state.onlyProblems && o.status === "complete") { return false; }
      if (!query) { return true; }
      return (o.name || "").toLowerCase().indexOf(query) >= 0 ||
             (o.kind || "").toLowerCase().indexOf(query) >= 0;
    });
  }

  function renderSidebar() {
    var list = $("#objectlist");
    var items = objectsFiltered();
    if (!items.length) {
      list.innerHTML = '<p class="empty">' + esc(t("ui.noMatch")) + '</p>';
      return;
    }
    var html = [], lastKind = null;
    items.forEach(function (o) {
      if (o.kind !== lastKind) {
        lastKind = o.kind;
        var count = items.filter(function (x) { return x.kind === o.kind; }).length;
        html.push('<div class="group-head">' + esc(kindLabel(o.kind)) +
          " (" + count + ")</div>");
      }
      html.push('<button class="item" data-id="' + esc(o.id) + '" ' +
        (state.objectId === o.id ? 'aria-current="true"' : "") + ">" +
        '<span class="dot ' + esc(o.status) + '" title="' + esc(statusLabel(o.status)) + '"></span>' +
        '<span class="name">' + esc(o.name) + "</span>" +
        '<span class="meta">' + esc(o.subtype ? t("kind.macro") : "") + "</span></button>");
    });
    list.innerHTML = html.join("");
    Array.prototype.forEach.call(list.querySelectorAll(".item"), function (button) {
      button.addEventListener("click", function () {
        state.objectId = button.getAttribute("data-id");
        state.diagramId = null;
        state.view = "object";
        state.tab = "overview";
        render();
      });
    });
  }

  /* ------------------------------------------------------------- dashboard */

  function renderDashboard() {
    var s = DATA.semantics, meta = DATA.meta;
    var html = [];
    html.push("<h1>" + esc(t("ui.dashTitle", { name: meta.file_name })) + "</h1>");
    html.push('<p class="muted">' + esc(t("ui.metaLine", {
      format: meta.format_description,
      count: meta.object_count,
      date: meta.generated
    })) + "</p>");

    var noteKey = DATA.meta.served_over_http ? "server" : "offline";
    html.push('<div class="note ok"><strong>' + esc(t("ui." + noteKey + "NoteStrong")) +
      "</strong>" + esc(t("ui." + noteKey + "NoteBody")) + "</div>");

    var stages = DATA.stages || {};
    var extraction = stages.extraction || {};
    var extracted = (extraction.counts || {}).complete || 0;
    if (meta.object_count && !extracted) {
      html.push('<div class="note bad"><strong>' + esc(t("ui.binaryWarnTitle")) +
        "</strong><br>" + esc(t("ui.binaryWarnBody", { count: meta.object_count })) +
        "<br><strong>" + esc(t("ui.binaryWarnEmphasis")) + "</strong> " +
        esc(t("ui.binaryWarnTail")) + "</div>");
    }

    if ((DATA.unprocessed || []).length) {
      html.push("<h2>" + esc(t("ui.unprocessedTitle")) + "</h2>");
      html.push('<p class="muted">' + esc(t("ui.unprocessedNote")) + "</p>");
      html.push('<div class="card"><table class="data"><thead><tr><th>' + esc(t("ui.colArea")) +
        "</th><th>" + esc(t("ui.colState")) + "</th><th>" + esc(t("ui.colReasonCode")) +
        "</th><th>" + esc(t("ui.colDetail")) + '</th><th class="num">' +
        esc(t("ui.colTarget")) + "</th></tr></thead><tbody>");
      DATA.unprocessed.forEach(function (u) {
        html.push("<tr><td><strong>" + esc(featureLabel(u.feature)) + "</strong></td><td>" +
          '<span class="badge ' + esc(u.status) + '">' + esc(statusLabel(u.status)) +
          "</span></td><td><code class=\"inline\">" + esc(u.reason_code) + "</code></td><td>" +
          esc(reasonText(u.reason_code, u.message)) + '</td><td class="num">' +
          esc(u.affected_object_count !== undefined ? u.affected_object_count
              : u.package_entry_count) + "</td></tr>");
      });
      html.push("</tbody></table></div>");
    }

    html.push("<h2>" + esc(t("ui.aspectTitle")) + "</h2>");
    html.push('<div class="grid cols-3">');
    ASPECT_KEYS.forEach(function (key) {
      var row = s.aspect_totals[key];
      if (!row) { return; }
      html.push('<div class="card"><h3 style="margin-top:0">' + esc(aspectLabel(key)) + "</h3>" +
        '<p class="muted" style="margin-top:-6px">' + esc(t("aspectDesc." + key)) + "</p>" +
        '<div style="font-size:38px;font-weight:800;line-height:1.1">' + pct(row.completion_percentage) + "</div>" +
        '<div class="bar" style="height:12px;border:1px solid var(--line);border-radius:999px;background:#fff;overflow:hidden;margin:8px 0">' +
        '<span style="display:block;height:100%;width:' + row.completion_percentage +
        '%;background:var(--ok)"></span></div>' +
        '<table class="data"><tbody>' +
        "<tr><td>" + esc(t("ui.aspectComplete")) + "</td><td class=\"num\">" + row.complete + "</td></tr>" +
        "<tr><td>" + esc(t("ui.aspectPartial")) + "</td><td class=\"num\">" + row.partial + "</td></tr>" +
        "<tr><td>" + esc(t("ui.aspectFailed")) + "</td><td class=\"num\">" + row.failed + "</td></tr>" +
        "<tr><td>" + esc(t("ui.aspectNA")) + "</td><td class=\"num\">" + row.not_applicable + "</td></tr>" +
        "</tbody></table></div>");
    });
    html.push("</div>");

    html.push("<h2>" + esc(t("ui.kindBreakdownTitle")) +
      '</h2><div class="card"><table class="data"><thead><tr>' +
      "<th>" + esc(t("ui.colKind")) + '</th><th class="num">' + esc(t("ui.colCount")) +
      '</th><th class="num">' + esc(t("ui.colFull")) + '</th><th class="num">' +
      esc(t("ui.colPart")) + '</th><th class="num">' + esc(t("ui.colFail")) +
      '</th><th class="num">' + esc(t("ui.colRate")) + "</th><th>" +
      esc(t("ui.colBlockers")) + "</th></tr></thead><tbody>");
    s.features.forEach(function (f) {
      html.push("<tr><td><strong>" + esc(kindLabel(f.kind)) +
        (f.subtype ? " (" + esc(f.subtype) + ")" : "") + "</strong></td>" +
        '<td class="num">' + f.total + '</td><td class="num">' + f.complete +
        '</td><td class="num">' + f.partial + '</td><td class="num">' + f.failed +
        '</td><td class="num">' + pct(f.completion_percentage) + "</td><td>" +
        f.blocking_reason_codes.slice(0, 3).map(function (r) {
          return '<span class="tag">' + esc(r.reason_code) + " ×" + r.objects + "</span>";
        }).join("") + "</td></tr>");
    });
    html.push("</tbody></table></div>");

    html.push("<h2>" + esc(t("ui.reasonTitle")) + "</h2>");
    html.push('<p class="muted">' + esc(t("ui.reasonNote")) + "</p>");
    html.push('<div class="card"><table class="data"><thead><tr><th>' + esc(t("ui.colReasonCode")) +
      "</th><th>" + esc(t("ui.colAspect")) + '</th><th class="num">' + esc(t("ui.colObjects")) +
      "</th><th>" + esc(t("ui.colDetail")) + "</th><th>" + esc(t("ui.colExamples")) +
      "</th></tr></thead><tbody>");
    s.reason_codes.forEach(function (r) {
      html.push("<tr><td><code class=\"inline\">" + esc(r.reason_code) + "</code></td>" +
        "<td>" + esc(aspectLabel(r.aspect)) + "</td>" +
        '<td class="num">' + r.objects + '</td><td title="' + esc(r.note) + '">' +
        esc(reasonText(r.reason_code, r.note)) + "</td><td class=\"muted\">" +
        r.examples.slice(0, 3).map(esc).join("<br>") + "</td></tr>");
    });
    html.push("</tbody></table></div>");

    if (DATA.flow.entry_points.length) {
      html.push("<h2>" + esc(t("ui.entryTitle")) +
        '</h2><div class="card"><table class="data"><tbody>');
      DATA.flow.entry_points.forEach(function (e) {
        html.push('<tr><td><button class="chip" data-goto="' + esc(e.id) + '">' +
          esc(e.name) + "</button></td><td>" +
          esc(e.why_key ? t("entry." + e.why_key) : (e.why || "")) + "</td></tr>");
      });
      html.push("</tbody></table></div>");
    }

    if (s.relationships.length) {
      html.push("<h2>" + esc(t("ui.relTitle", { count: s.relationships.length })) + "</h2>");
      html.push('<div class="card"><table class="data"><thead><tr><th>' +
        esc(t("ui.colChildTable")) + "</th><th>" + esc(t("ui.colColumns")) + "</th><th>" +
        esc(t("ui.colParentTable")) + "</th><th>" + esc(t("ui.colColumns")) + "</th><th>" +
        esc(t("ui.colIntegrity")) + "</th></tr></thead><tbody>");
      s.relationships.forEach(function (r) {
        html.push("<tr><td>" + esc(r.child_table) + "</td><td>" + esc(r.child_columns.join(", ")) +
          "</td><td>" + esc(r.parent_table) + "</td><td>" + esc(r.parent_columns.join(", ")) +
          "</td><td>" + esc(r.enforced ? t("ui.yes") : t("ui.no")) +
          (r.cascade_delete ? " / " + esc(t("ui.cascadeDelete")) : "") +
          (r.cascade_update ? " / " + esc(t("ui.cascadeUpdate")) : "") +
          "</td></tr>");
      });
      html.push("</tbody></table></div>");
    }
    return html.join("");
  }

  /* -------------------------------------------------------- object details */

  function objectDetail(id) { return DATA.details[id] || null; }
  function objectMeta(id) {
    return DATA.flow.objects.filter(function (o) { return o.id === id; })[0] || null;
  }

  function diagramsFor(id) {
    var meta = objectMeta(id);
    if (!meta) { return []; }
    var result = [];
    if (meta.kind === "macro" && meta.subtype === "data_macro") {
      Object.keys(DATA.flow.diagrams).forEach(function (key) {
        if (key.indexOf("datamacro::" + meta.name + "::") === 0) {
          result.push({
            id: key,
            label: t("ui.dataMacroDiagLabel", { event: key.split("::")[2] })
          });
        }
      });
    } else if (meta.kind === "macro") {
      if (DATA.flow.diagrams["macro::" + meta.name]) {
        result.push({
          id: "macro::" + meta.name,
          label: t("ui.macroDiagLabel", { name: meta.name })
        });
      }
    }
    DATA.flow.procedures.forEach(function (p) {
      if (p.owner === meta.name && (p.owner_kind === meta.kind ||
          (meta.kind === "module" && p.owner_kind === "module"))) {
        result.push({
          id: p.id,
          label: t("ui.procDiagLabel", {
            name: p.name,
            kind: p.kind,
            branches: p.metrics.branches || 0,
            loops: p.metrics.loops || 0
          })
        });
      }
    });
    return result;
  }

  function renderObject() {
    var meta = objectMeta(state.objectId);
    if (!meta) { return '<p class="empty">' + esc(t("ui.selectPrompt")) + "</p>"; }
    var detail = objectDetail(state.objectId) || {};
    var tabs = [
      ["overview", t("ui.tabOverview")],
      ["flow", t("ui.tabFlow")],
      ["translated", t("ui.tabTranslated")],
      ["blockers", t("ui.tabBlockers")],
      ["source", t("ui.tabSource")]
    ];
    var html = [];
    html.push("<h1>" + esc(meta.name) + ' <span class="tag k-' + esc(meta.kind) + '">' +
      esc(kindLabel(meta.kind)) + "</span></h1>");
    html.push("<p>");
    ASPECT_KEYS.forEach(function (key) {
      var status = (meta.aspects || {})[key] || "not_applicable";
      html.push('<span class="badge ' + esc(status) + '" style="margin-right:8px">' +
        esc(aspectLabel(key)) + ": " + esc(statusLabel(status)) + "</span>");
    });
    html.push("</p>");
    html.push('<div class="tabs">' + tabs.map(function (t) {
      return '<button data-tab="' + t[0] + '" aria-selected="' + (state.tab === t[0]) + '">' +
        t[1] + "</button>";
    }).join("") + "</div>");
    html.push('<div id="tabbody">' + renderObjectTab(meta, detail) + "</div>");
    return html.join("");
  }

  function kvTable(pairs) {
    return '<dl class="kv">' + pairs.filter(function (p) {
      return p[1] !== undefined && p[1] !== null && p[1] !== "";
    }).map(function (p) {
      return "<dt>" + esc(p[0]) + "</dt><dd>" + p[1] + "</dd>";
    }).join("") + "</dl>";
  }

  function renderObjectTab(meta, detail) {
    if (state.tab === "flow") { return '<div id="objflow"></div>'; }
    if (state.tab === "source") {
      if (!detail.source_text) {
        return '<div class="note">' + esc(t("ui.noSourceText")) + "</div>";
      }
      return '<pre class="code" style="max-height:70vh">' + esc(detail.source_text) + "</pre>";
    }
    if (state.tab === "blockers") { return renderBlockers(meta); }
    if (state.tab === "translated") { return renderTranslated(meta, detail); }
    return renderOverview(meta, detail);
  }

  function renderOverview(meta, detail) {
    var html = [];
    var summary = meta.summary || {};
    var pairs = [[t("ui.kindLabel"), esc(kindLabel(meta.kind))]];
    Object.keys(summary).forEach(function (key) {
      var value = summary[key];
      if (value && typeof value === "object") { value = JSON.stringify(value); }
      pairs.push([key, esc(value)]);
    });
    html.push('<div class="card">' + kvTable(pairs) + "</div>");

    if (meta.kind === "form" || meta.kind === "report") {
      html.push(renderScreenSketch(detail));
      if (detail.events && detail.events.length) {
        html.push("<h2>" + esc(t("ui.eventTitle")) +
          '</h2><div class="card"><table class="data"><thead><tr>' +
          "<th>" + esc(t("ui.colEvent")) + "</th><th>" + esc(t("ui.colHandlerKind")) +
          "</th><th>" + esc(t("ui.colHandler")) + "</th></tr></thead><tbody>");
        detail.events.forEach(function (e) {
          html.push("<tr><td><code class=\"inline\">" + esc(e.event) + "</code></td><td>" +
            esc({
              vba_event_procedure: t("ui.handlerVba"),
              macro_object: t("ui.handlerMacro"),
              expression: t("ui.handlerExpr"),
              embedded_macro: t("ui.handlerEmbedded")
            }[e.handler_kind] || e.handler_kind) +
            "</td><td>" + esc(e.handler || t("ui.handlerNone")) + "</td></tr>");
        });
        html.push("</tbody></table></div>");
      }
    }
    if (meta.kind === "table" && detail.columns) {
      html.push("<h2>" + esc(t("ui.columnsTitle", { count: detail.columns.length })) +
        '</h2><div class="card"><table class="data"><thead><tr><th>' +
        esc(t("ui.colColumnName")) + "</th><th>" + esc(t("ui.colAccessType")) + "</th><th>" +
        esc(t("ui.colTargetType")) + "</th><th>" + esc(t("ui.colNull")) + "</th><th>" +
        esc(t("ui.colDefault")) + "</th><th>" + esc(t("ui.colValidation")) +
        "</th></tr></thead><tbody>");
      detail.columns.forEach(function (c) {
        html.push("<tr><td><strong>" + esc(c.name) + "</strong>" +
          (detail.primary_key.indexOf(c.name) >= 0 ? ' <span class="tag">PK</span>' : "") +
          "</td><td>" + esc(c.jet_type) + "</td><td>" +
          (c.sql_type ? esc(c.sql_type)
            : '<span class="badge failed">' + esc(t("ui.typeUnconvertible")) + "</span>") +
          "</td><td>" + esc(c.nullable ? t("ui.nullYes") : t("ui.nullNo")) + "</td><td>" +
          esc(c.default ? c.default.sql : "") + "</td><td>" +
          esc(c.validation ? c.validation.sql : "") + "</td></tr>");
      });
      html.push("</tbody></table></div>");
    }
    if (meta.kind === "module" && detail.procedures) {
      html.push("<h2>" + esc(t("ui.proceduresTitle", { count: detail.procedures.length })) +
        '</h2><div class="card"><table class="data"><thead><tr><th>' + esc(t("ui.colName")) +
        "</th><th>" + esc(t("ui.colType")) + "</th><th>" + esc(t("ui.colScope")) +
        '</th><th class="num">' + esc(t("ui.colComplexity")) + "</th><th>" +
        esc(t("ui.colEffects")) + "</th></tr></thead><tbody>");
      detail.procedures.forEach(function (p) {
        html.push("<tr><td><strong>" + esc(p.name) + "</strong></td><td>" + esc(p.kind) +
          "</td><td>" + esc(p.scope) + '</td><td class="num">' +
          (p.metrics ? p.metrics.cyclomatic_complexity : "") + "</td><td>" +
          (p.effects.external_effects || []).map(function (e) {
            return '<span class="tag">' + esc(e.reason_code) + "</span>";
          }).join("") + "</td></tr>");
      });
      html.push("</tbody></table></div>");
    }
    return html.join("");
  }

  function renderScreenSketch(detail) {
    if (!detail.sections || !detail.sections.length) { return ""; }
    var scale = 0.045, y = 0, parts = [], width = 0;
    detail.sections.forEach(function (section) {
      section.controls.forEach(function collect(c) {
        var g = c.geometry || {};
        width = Math.max(width, (g.left || 0) + (g.width || 0));
        (c.children || []).forEach(collect);
      });
    });
    width = Math.max(width, 6000);
    detail.sections.forEach(function (section) {
      var height = parseInt(section.height || "1200", 10) || 1200;
      parts.push('<div class="sec" style="top:' + (y * scale) + 'px"></div>');
      parts.push('<div class="seclabel" style="top:' + (y * scale + 2) + 'px">' +
        esc(section.name) + "</div>");
      section.controls.forEach(function place(c) {
        var g = c.geometry || {};
        if (g.width && g.height) {
          var bound = (c.bindings || []).length > 0;
          var evented = (c.events || []).length > 0;
          parts.push('<div class="ctl' + (bound ? " bound" : "") + (evented ? " event" : "") +
            '" style="left:' + ((g.left || 0) * scale) + "px;top:" + ((y + (g.top || 0)) * scale) +
            "px;width:" + (g.width * scale) + "px;height:" + (g.height * scale) + 'px" title="' +
            esc(c.name + " / " + c.control_type) + '">' + esc(c.name || "") + "</div>");
        }
        (c.children || []).forEach(place);
      });
      y += height + 300;
    });
    return "<h2>" + esc(t("ui.screenTitle")) + '</h2><p class="muted">' +
      esc(t("ui.screenNote")) + "</p>" +
      '<div class="screen-sketch" style="width:' + Math.min(width * scale + 20, 900) +
      "px;height:" + (y * scale + 20) + 'px">' + parts.join("") + "</div>";
  }

  function renderTranslated(meta, detail) {
    var html = [];
    if (detail.sql) {
      html.push("<h2>" + esc(t("ui.sqlTitle")) + '</h2><p class="muted">' +
        esc(t("ui.sqlNote")) + "</p>" +
        '<pre class="code">' + esc(detail.sql) + "</pre>");
    }
    if (detail.ddl) {
      html.push("<h2>" + esc(t("ui.ddlTitle")) + '</h2><pre class="code">' +
        esc(detail.ddl) + "</pre>");
    }
    if (detail.joins && detail.joins.length) {
      html.push("<h2>" + esc(t("ui.joinTitle")) +
        '</h2><div class="card"><table class="data"><thead><tr><th>' + esc(t("ui.colLeft")) +
        "</th><th>" + esc(t("ui.colRight")) + "</th><th>" + esc(t("ui.colJoinType")) +
        "</th><th>" + esc(t("ui.colOn")) + "</th></tr></thead><tbody>");
      detail.joins.forEach(function (j) {
        html.push("<tr><td>" + esc(j.left_table) + "</td><td>" + esc(j.right_table) + "</td><td>" +
          esc(j.join_type) + "</td><td><code class=\"inline\">" + esc(j.on) + "</code></td></tr>");
      });
      html.push("</tbody></table></div>");
    }
    if (detail.handlers) {
      detail.handlers.forEach(function (h) {
        html.push("<h2>" + esc(h.event) + "（" + esc(h.trigger_timing) + " / " +
          esc(h.trigger_operation) + "）</h2>");
        html.push('<div class="card">' + renderStatements(h.statements) + "</div>");
      });
    }
    if (detail.statements && meta.kind === "macro") {
      html.push("<h2>" + esc(t("ui.macroStepsTitle")) + '</h2><div class="card">' +
        renderStatements(detail.statements) + "</div>");
    }
    if (!html.length) {
      html.push('<div class="note">' + esc(t("ui.noArtifacts")) + "</div>");
    }
    return html.join("");
  }

  function renderStatements(statements, depth) {
    depth = depth || 0;
    if (!statements || !statements.length) {
      return '<p class="muted">' + esc(t("ui.noStatements")) + "</p>";
    }
    var html = ["<ul style=\"margin:4px 0;padding-left:" + (depth ? 22 : 18) + "px\">"];
    statements.forEach(function (s) {
      if (s.type === "comment") {
        html.push('<li class="muted">※ ' + esc(s.text) + "</li>");
      } else if (s.type === "action") {
        var args = s.arguments ? Object.keys(s.arguments).map(function (k) {
          return k + "=" + s.arguments[k].source;
        }).join(", ") : (s.positional_arguments || []).map(function (a) { return a.source; })
          .filter(Boolean).join(", ");
        html.push("<li><strong>" + esc(s.action) + "</strong>" +
          (args ? " <code class=\"inline\">" + esc(args) + "</code>" : "") +
          ' <span class="badge ' + (s.translated ? "complete" : "partial") + '">' +
          esc(s.category) + "</span></li>");
      } else if (s.type === "conditional") {
        html.push("<li><strong>" + esc(t("ui.branchTitle")) +
          '</strong><ul style="padding-left:18px">');
        s.branches.forEach(function (b) {
          // The condition is escaped first and then placed into the
          // translated sentence, because the placeholder is not at the end of
          // that sentence in every language.
          var condition = b.condition
            ? t("ui.conditionIf", {
                cond: '<code class="inline">' + esc(b.condition.source) + "</code>"
              })
            : esc(t("ui.conditionElse"));
          html.push("<li>" + condition +
            renderStatements(b.statements, depth + 1) + "</li>");
        });
        html.push("</ul></li>");
      } else if (s.type === "block") {
        html.push("<li><strong>" + esc(s.block) + "</strong>" +
          renderStatements(s.statements, depth + 1) + "</li>");
      } else {
        html.push("<li>" + esc(s.element || s.type) + "</li>");
      }
    });
    html.push("</ul>");
    return html.join("");
  }

  function renderBlockers(meta) {
    var html = [];
    if (!meta.blockers.length && !meta.advisories.length) {
      return '<div class="note ok"><strong>' + esc(t("ui.blockersNoneStrong")) +
        "</strong>" + esc(t("ui.blockersNoneBody")) + "</div>";
    }
    if (meta.blockers.length) {
      html.push("<h2>" + esc(t("ui.blockersTitle", { count: meta.blockers.length })) + "</h2>");
      html.push('<div class="card"><table class="data"><thead><tr><th>' +
        esc(t("ui.colReasonCode")) + "</th><th>" + esc(t("ui.colAspect")) + "</th><th>" +
        esc(t("ui.colTarget")) + "</th><th>" + esc(t("ui.colWhy")) + "</th></tr></thead><tbody>");
      meta.blockers.forEach(function (b) {
        html.push("<tr><td><code class=\"inline\">" + esc(b.reason_code) + "</code></td><td>" +
          esc(DATA.aspect_of[b.reason_code]
            ? aspectLabel(DATA.aspect_of[b.reason_code]) : "-") + "</td><td>" +
          esc(b.detail) + '</td><td title="' + esc(b.note) + '">' +
          esc(reasonText(b.reason_code, b.note)) + "</td></tr>");
      });
      html.push("</tbody></table></div>");
    }
    if (meta.advisories.length) {
      html.push("<h2>" + esc(t("ui.advisoriesTitle", { count: meta.advisories.length })) + "</h2>");
      html.push('<p class="muted">' + esc(t("ui.advisoriesNote")) + "</p>");
      html.push('<div class="card"><table class="data"><thead><tr><th>' +
        esc(t("ui.colReasonCode")) + "</th><th>" + esc(t("ui.colTarget")) + "</th><th>" +
        esc(t("ui.colDetail")) + "</th></tr></thead><tbody>");
      meta.advisories.forEach(function (b) {
        html.push("<tr><td><code class=\"inline\">" + esc(b.reason_code) + "</code></td><td>" +
          esc(b.detail) + '</td><td title="' + esc(b.note) + '">' +
          esc(reasonText(b.reason_code, b.note)) + "</td></tr>");
      });
      html.push("</tbody></table></div>");
    }
    return html.join("");
  }

  /* ------------------------------------------------------------- system map */

  function neighbourhood(rootId, depth) {
    var graph = DATA.flow.graph;
    if (!rootId) { return graph; }
    var keep = {}, frontier = [rootId];
    keep[rootId] = true;
    for (var step = 0; step < depth; step++) {
      var next = [];
      graph.edges.forEach(function (e) {
        if (keep[e.from] && !keep[e.to]) { keep[e.to] = true; next.push(e.to); }
        else if (keep[e.to] && !keep[e.from]) { keep[e.from] = true; next.push(e.from); }
      });
      frontier = next;
      if (!frontier.length) { break; }
    }
    return {
      nodes: graph.nodes.filter(function (n) { return keep[n.id]; }),
      edges: graph.edges.filter(function (e) { return keep[e.from] && keep[e.to]; })
    };
  }

  /* ---------------------------------------------------------------- render */

  function render() {
    $("#viewtabs").querySelectorAll("button").forEach(function (b) {
      b.setAttribute("aria-selected", String(b.getAttribute("data-view") === state.view));
    });
    document.querySelector("main.body").classList.toggle("wide", state.view === "map");
    renderSidebar();
    var content = $("#content");
    content.classList.toggle("flush", state.view === "map" ||
      (state.view === "object" && state.tab === "flow"));

    if (state.view === "dashboard") {
      content.innerHTML = renderDashboard();
      content.querySelectorAll("[data-goto]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.objectId = button.getAttribute("data-goto");
          state.view = "object"; state.tab = "overview"; state.diagramId = null;
          render();
        });
      });
      return;
    }
    if (state.view === "map") { renderMap(content); return; }

    content.innerHTML = renderObject();
    content.querySelectorAll(".tabs button").forEach(function (button) {
      button.addEventListener("click", function () {
        state.tab = button.getAttribute("data-tab");
        render();
      });
    });
    if (state.tab === "flow") { renderObjectFlow(); }
  }

  function renderObjectFlow() {
    var host = $("#objflow");
    if (!host) { return; }
    var options = diagramsFor(state.objectId);
    if (!options.length) {
      host.innerHTML = '<div class="empty">' + esc(t("ui.noProcedures")) + "</div>";
      return;
    }
    if (!state.diagramId || options.every(function (o) { return o.id !== state.diagramId; })) {
      state.diagramId = options[0].id;
    }
    host.innerHTML =
      '<div class="canvaswrap" style="height:calc(100vh - 250px)">' +
      '<div class="toolbar"><label>' + esc(t("ui.flowPick")) + '<select id="diagpick">' +
      options.map(function (o) {
        return '<option value="' + esc(o.id) + '"' +
          (o.id === state.diagramId ? " selected" : "") + ">" + esc(o.label) + "</option>";
      }).join("") + "</select></label>" +
      '<button data-act="fit">' + esc(t("ui.btnFit")) + '</button><button data-act="in">' +
      esc(t("ui.btnZoomIn")) + '</button><button data-act="out">' + esc(t("ui.btnZoomOut")) +
      '</button><label><input type="checkbox" id="horiz"> ' + esc(t("ui.chkHoriz")) + "</label>" +
      '<span class="hint">' + esc(t("ui.canvasHint")) + "</span>" +
      '<span class="legend" style="margin-left:auto">' +
      '<span class="key"><i style="background:#fdefda;border-color:#9a5b00"></i>' +
      esc(t("ui.legendDecision")) +
      '</span><span class="key"><i style="background:#e3f0ff;border-color:#14458f"></i>' +
      esc(t("ui.legendData")) +
      '</span><span class="key"><i style="background:#f3e8fd;border-color:#6b21a8"></i>' +
      esc(t("ui.legendUi")) +
      '</span><span class="key"><i style="background:#fce8e6;border-color:#a51f14"></i>' +
      esc(t("ui.legendError")) + "</span>" +
      "</span></div>" +
      '<div class="stage" id="stage"></div></div>';
    drawObjectDiagram();
    $("#diagpick").addEventListener("change", function (event) {
      state.diagramId = event.target.value;
      drawObjectDiagram();
    });
    $("#horiz").addEventListener("change", drawObjectDiagram);
    host.querySelectorAll("[data-act]").forEach(function (button) {
      button.addEventListener("click", function () {
        var stage = $("#stage");
        var act = button.getAttribute("data-act");
        if (act === "fit") { stage.__fit(); }
        else { stage.__zoom(act === "in" ? 1.25 : 0.8); }
      });
    });
  }

  function drawObjectDiagram() {
    var stage = $("#stage");
    var graph = DATA.flow.diagrams[state.diagramId];
    if (!graph) {
      stage.innerHTML = '<div class="empty">' + esc(t("ui.noDiagram")) + "</div>";
      return;
    }
    var horizontal = $("#horiz") && $("#horiz").checked;
    var laid = renderDiagram(stage, graph, { horizontal: horizontal });
    attachStage(stage, laid, function (nodeId) {
      var node = graph.nodes.filter(function (n) { return n.id === nodeId; })[0];
      if (!node) { return; }
      showInspector(stage,
        nodeText(node) || node.kind,
        kvTable([
          [t("ui.inspShape"), esc(node.kind)],
          [t("ui.inspLine"), node.line ? esc(node.line) : ""],
          [t("ui.inspCategory"), node.category ? esc(node.category) : ""],
          [t("ui.inspTranslatable"), node.translated === undefined ? ""
            : esc(node.translated ? t("ui.yesCan") : t("ui.noCan"))]
        ]));
    });
  }

  function showInspector(stage, title, bodyHtml) {
    var existing = stage.parentNode.querySelector(".inspector");
    if (existing) { existing.remove(); }
    var box = document.createElement("div");
    box.className = "inspector";
    box.innerHTML = '<button class="close" aria-label="' + esc(t("ui.close")) +
      '">\u00d7</button><h3>' +
      esc(title) + "</h3>" + bodyHtml;
    stage.parentNode.appendChild(box);
    box.querySelector(".close").addEventListener("click", function () { box.remove(); });
  }

  function renderMap(content) {
    var candidates = DATA.flow.graph.nodes.slice().sort(function (a, b) {
      return (a.kind + a.label).localeCompare(b.kind + b.label);
    });
    if (!state.mapRoot) {
      state.mapRoot = (DATA.flow.entry_points[0] || candidates[0] || {}).id || null;
    }
    content.innerHTML =
      '<div class="canvaswrap" style="height:calc(100vh - 96px)">' +
      '<div class="toolbar"><label>' + esc(t("ui.mapRoot")) + '<select id="maproot">' +
      candidates.map(function (n) {
        return '<option value="' + esc(n.id) + '"' + (n.id === state.mapRoot ? " selected" : "") +
          ">" + esc(kindLabel(n.kind) + ": " + n.label) + "</option>";
      }).join("") + "</select></label>" +
      "<label>" + esc(t("ui.mapDepth")) + '<select id="mapdepth">' +
      [1, 2, 3, 4].map(function (d) {
        return '<option value="' + d + '"' + (d === state.mapDepth ? " selected" : "") + ">" + d + "</option>";
      }).join("") + "</select></label>" +
      '<button data-act="fit">' + esc(t("ui.btnFit")) + '</button><button data-act="in">' +
      esc(t("ui.btnZoomIn")) + '</button><button data-act="out">' + esc(t("ui.btnZoomOut")) +
      '</button><button data-act="all">' + esc(t("ui.btnShowAll")) + "</button>" +
      '<span class="hint">' + esc(t("ui.mapHint")) + "</span>" +
      "</div><div class=\"stage\" id=\"stage\"></div></div>";

    drawMap();
    $("#maproot").addEventListener("change", function (e) { state.mapRoot = e.target.value; drawMap(); });
    $("#mapdepth").addEventListener("change", function (e) {
      state.mapDepth = parseInt(e.target.value, 10); drawMap();
    });
    content.querySelectorAll("[data-act]").forEach(function (button) {
      button.addEventListener("click", function () {
        var stage = $("#stage");
        var act = button.getAttribute("data-act");
        if (act === "fit") { stage.__fit(); }
        else if (act === "all") { state.mapRoot = null; drawMap(); }
        else { stage.__zoom(act === "in" ? 1.25 : 0.8); }
      });
    });
  }

  function drawMap() {
    var stage = $("#stage");
    var graph = neighbourhood(state.mapRoot, state.mapDepth);
    if (graph.nodes.length > 400) {
      stage.innerHTML = '<div class="empty">' +
        esc(t("ui.mapTooMany", { count: graph.nodes.length })) + "</div>";
      return;
    }
    var laid = renderDiagram(stage, graph, { horizontal: true });
    attachStage(stage, laid, function (nodeId) {
      var node = graph.nodes.filter(function (n) { return n.id === nodeId; })[0];
      if (!node) { return; }
      var links = DATA.flow.graph.edges.filter(function (e) {
        return e.from === nodeId || e.to === nodeId;
      });
      var body = kvTable([
        [t("ui.kindLabel"), esc(kindLabel(node.kind))],
        [t("ui.statusLabel"), esc(statusLabel(node.status))]
      ]) + "<h3>" + esc(t("ui.links")) + "</h3><ul>" + links.slice(0, 30).map(function (e) {
        var other = e.from === nodeId ? e.to : e.from;
        var direction = e.from === nodeId ? "→" : "←";
        var target = DATA.flow.graph.nodes.filter(function (n) { return n.id === other; })[0];
        return "<li>" + direction + " " + esc(target ? target.label : other) +
          ' <span class="tag">' + esc(t("edgeKind." + e.kind) === "edgeKind." + e.kind
            ? e.kind : t("edgeKind." + e.kind)) + "</span></li>";
      }).join("") + "</ul>" +
      (DATA.details[nodeId]
        ? '<button class="chip" id="openobj">' + esc(t("ui.openObject")) + "</button>" : "");
      showInspector(stage, node.label, body);
      var open = document.getElementById("openobj");
      if (open) {
        open.addEventListener("click", function () {
          state.objectId = nodeId; state.view = "object"; state.tab = "overview";
          state.diagramId = null; render();
        });
      }
    });
  }

  /* ------------------------------------------------------------------ boot */

  function renderGauges() {
    var totals = DATA.semantics.aspect_totals || {};
    $("#gauges").innerHTML = ASPECT_KEYS.map(function (key) {
      var row = totals[key];
      if (!row) { return ""; }
      return '<div class="gauge"><span class="label">' + esc(aspectLabel(key)) + "</span>" +
        '<span class="value">' + pct(row.completion_percentage) + "</span>" +
        '<div class="bar"><span style="width:' + row.completion_percentage + '%"></span></div>' +
        '<span class="detail">' + row.complete + " / " + row.scored_objects + "</span></div>";
    }).join("");
  }

  /** Paint every label that lives in the page shell rather than in a view. */
  function applyChrome() {
    document.documentElement.lang = state.lang;
    var subtitle = $("#subtitle");
    if (subtitle) {
      subtitle.textContent = t(
        DATA.meta.served_over_http ? "ui.subtitleWeb" : "ui.subtitleOffline"
      );
    }
    var search = $("#search");
    if (search) {
      search.placeholder = t("ui.searchPlaceholder");
      search.setAttribute("aria-label", t("ui.searchLabel"));
    }
    var langLabel = $("#langlabel");
    if (langLabel) { langLabel.textContent = t("ui.language"); }
    $("#viewtabs").querySelectorAll("button").forEach(function (button) {
      var view = button.getAttribute("data-view");
      button.textContent = t("ui.view" + view.charAt(0).toUpperCase() + view.slice(1));
    });
  }

  function buildFilters() {
    $("#filters").innerHTML = Object.keys(state.kinds).map(function (k) {
      return '<button class="chip" data-kind="' + esc(k) + '" aria-pressed="' +
        String(state.kinds[k] !== false) + '">' + esc(kindLabel(k)) + "</button>";
    }).join("") +
      '<button class="chip" id="onlyproblems" aria-pressed="' + String(state.onlyProblems) +
      '">' + esc(t("ui.onlyProblems")) + "</button>";

    $("#filters").querySelectorAll("[data-kind]").forEach(function (button) {
      button.addEventListener("click", function () {
        var kind = button.getAttribute("data-kind");
        state.kinds[kind] = !state.kinds[kind];
        button.setAttribute("aria-pressed", String(state.kinds[kind]));
        renderSidebar();
      });
    });
    $("#onlyproblems").addEventListener("click", function () {
      state.onlyProblems = !state.onlyProblems;
      this.setAttribute("aria-pressed", String(state.onlyProblems));
      renderSidebar();
    });
  }

  function setLanguage(code) {
    state.lang = code;
    I18N.remember(code);
    if (typeof window.__ACCESS_ON_LANGUAGE__ === "function") {
      window.__ACCESS_ON_LANGUAGE__(code);
    }
    applyChrome();
    buildFilters();
    renderGauges();
    render();
  }

  function boot() {
    var kinds = {};
    DATA.flow.objects.forEach(function (o) { kinds[o.kind] = true; });
    state.kinds = kinds;

    $("#langpick").innerHTML = I18N.languages.map(function (language) {
      return '<option value="' + esc(language.code) + '"' +
        (language.code === state.lang ? " selected" : "") + ">" +
        esc(language.label) + "</option>";
    }).join("");
    $("#langpick").addEventListener("change", function (event) {
      setLanguage(event.target.value);
    });

    buildFilters();
    $("#search").addEventListener("input", function (event) {
      state.query = event.target.value; renderSidebar();
    });
    $("#viewtabs").querySelectorAll("button").forEach(function (button) {
      button.addEventListener("click", function () {
        state.view = button.getAttribute("data-view");
        render();
      });
    });
    applyChrome();
    renderGauges();
    render();
  }

  /* The viewer is a component, not a page: the web front end hands it a
     payload after an upload, and the offline file hands it one at load time. */
  window.__ACCESS_VIEWER__ = {
    start: function (payload) {
      DATA = payload;
      boot();
    },
    t: t,
    languages: I18N.languages,
    currentLanguage: function () { return state.lang; },
    setLanguage: function (code) {
      state.lang = code;
      I18N.remember(code);
      if (DATA) { setLanguage(code); }
      if (typeof window.__ACCESS_ON_LANGUAGE__ === "function") {
        window.__ACCESS_ON_LANGUAGE__(code);
      }
    }
  };

  if (DATA) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", boot);
    } else { boot(); }
  }
})();
