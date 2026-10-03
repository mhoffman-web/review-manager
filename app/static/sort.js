// Click-to-sort for every table with a <thead>. Numeric-aware, '–'/empty last,
// third click restores the original order. Tables with data-server-sort are
// skipped (their headers are links that sort on the server instead). Cells can
// provide data-sort="<value>" when the visible text is not what should sort.
// Multi-row headers (rowspan/colspan) are mapped to their real column, and
// only <tbody> rows move: totals belong in <tfoot> and stay put.
(function () {
  function keyOf(td) {
    if (!td) return { n: null, s: "" };
    var raw = td.dataset.sort != null ? td.dataset.sort : td.textContent;
    var s = String(raw).trim();
    if (s === "" || s === "–" || s === "-" || s === "—") return { n: null, s: "" };
    var cleaned = s.replace(/[%$,★×]/g, "").replace(/\s+/g, "");
    var m = cleaned.match(/^(-?\d+(?:\.\d+)?)([hdm])?$/);
    if (m) {
      var n = parseFloat(m[1]);
      if (m[2] === "d") n *= 24; else if (m[2] === "m") n /= 60;   // durations like 33h / 1.4d / 45m
      return { n: n, s: s.toLowerCase() };
    }
    var ago = cleaned.match(/^(\d+)([hdm])ago$/);
    if (ago) { var v = parseInt(ago[1], 10); if (ago[2] === "d") v *= 24; else if (ago[2] === "m") v /= 60; return { n: v, s: s.toLowerCase() }; }
    return { n: null, s: s.toLowerCase() };
  }
  // Header cells that name exactly one column, with that column's index.
  function columnHeaders(thead) {
    var grid = [], out = [];
    Array.prototype.forEach.call(thead.rows, function (tr, r) {
      grid[r] = grid[r] || [];
      var c = 0;
      Array.prototype.forEach.call(tr.cells, function (th) {
        while (grid[r][c]) c++;
        var rs = th.rowSpan || 1, cs = th.colSpan || 1;
        for (var i = 0; i < rs; i++) { grid[r + i] = grid[r + i] || []; for (var j = 0; j < cs; j++) grid[r + i][c + j] = true; }
        if (cs === 1) out.push({ th: th, col: c });
        c += cs;
      });
    });
    var width = 0; grid.forEach(function (row) { width = Math.max(width, row.length); });
    return { cells: out, width: width };
  }
  function setup(table) {
    var thead = table.tHead, tbody = table.tBodies[0];
    if (!thead || !tbody) return;
    var original = Array.prototype.slice.call(tbody.rows);
    var map = columnHeaders(thead), heads = map.cells.map(function (h) { return h.th; });
    map.cells.forEach(function (h) {
      var th = h.th, idx = h.col;
      if (th.hasAttribute("data-nosort") || !th.textContent.trim()) return;
      th.classList.add("sortable");
      th.setAttribute("title", "Click to sort");
      th.addEventListener("click", function (e) {
        if (e.target.closest("a,button,input,select")) return;
        var state = th.getAttribute("aria-sort");           // none -> ascending -> descending -> none
        heads.forEach(function (o) { o.removeAttribute("aria-sort"); });
        if (state === "descending") {
          original.forEach(function (r) { tbody.appendChild(r); r.classList.remove("sort-hidden"); });
          return;
        }
        var dir = state === "ascending" ? -1 : 1;
        th.setAttribute("aria-sort", dir === 1 ? "ascending" : "descending");
        var rows = original.filter(function (r) {
          var group = r.classList.contains("brand-row") || r.classList.contains("total-row") || r.cells.length < map.width;
          if (group) r.classList.add("sort-hidden");           // group header rows do not survive a sort
          return !group;
        });
        rows.sort(function (a, b) {
          var ka = keyOf(a.cells[idx]), kb = keyOf(b.cells[idx]);
          if (ka.n === null && kb.n === null && !ka.s && !kb.s) return 0;
          if (ka.n === null && !ka.s) return 1;                // empties last regardless of direction
          if (kb.n === null && !kb.s) return -1;
          if (ka.n !== null && kb.n !== null) return (ka.n - kb.n) * dir;
          if (ka.n !== null) return -1 * dir;
          if (kb.n !== null) return 1 * dir;
          return ka.s.localeCompare(kb.s) * dir;
        });
        rows.forEach(function (r) { tbody.appendChild(r); });
      });
    });
  }
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("main table").forEach(function (t) {
      if (!t.hasAttribute("data-server-sort")) setup(t);
    });
  });
})();
