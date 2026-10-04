// Keyboard shortcuts. Inbox: J/K (or arrows once a row is selected) select, Enter/O open,
// R reply inline, E archive, 1-9 template, / search. Review page: R focus reply, E archive,
// N/P next/prev, 1-9 template. Alt+1-9 picks a template even while typing in a reply.
// ? toggles help. Esc closes things. Shortcuts never fire from a focused button or link.
(function () {
  var help = document.getElementById('keysHelp');
  function typing(e) { var t = e.target; return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable); }
  function onControl(e) { return !!(e.target && e.target.closest && e.target.closest('a[href],button,summary,[role="button"],[role="tab"],label')); }
  var lastFocus = null;
  function openHelp() {
    if (!help) return;
    lastFocus = document.activeElement; help.hidden = false;
    var f = help.querySelector('button, [href], [tabindex]:not([tabindex="-1"])') || help;
    if (f === help) help.setAttribute('tabindex', '-1');
    f.focus();
  }
  function closeHelp() { if (!help || help.hidden) return; help.hidden = true; if (lastFocus && lastFocus.focus) lastFocus.focus(); }
  function toggleHelp() { if (help && help.hidden) openHelp(); else closeHelp(); }
  document.querySelectorAll('[data-keys-help]').forEach(function (b) { b.addEventListener('click', toggleHelp); });
  if (help) {
    help.addEventListener('click', function (e) { if (e.target === help || e.target.closest('[data-close]')) closeHelp(); });
    help.addEventListener('keydown', function (e) {           // keep Tab inside the open panel
      if (e.key !== 'Tab') return;
      var f = [].slice.call(help.querySelectorAll('button, [href], input, [tabindex]:not([tabindex="-1"])'));
      if (!f.length) { e.preventDefault(); return; }
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
  }

  var rows = function () { return [].slice.call(document.querySelectorAll('#inboxTable tr.clickable[data-id]')); };
  var sel = -1;
  function select(i) {
    var rs = rows(); if (!rs.length) return;
    sel = Math.max(0, Math.min(rs.length - 1, i));
    rs.forEach(function (r, k) { r.classList.toggle('kb-sel', k === sel); });
    rs[sel].scrollIntoView({ block: 'nearest' });
  }
  function current() { var rs = rows(); return sel >= 0 && sel < rs.length ? rs[sel] : null; }
  function chipOnPage(n) { var chips = document.querySelectorAll('#reply .chip[data-tpl]'); var b = chips[n - 1]; if (b) b.click(); }
  function pickTemplate(n) {
    if (window.RMinbox && RMinbox.isOpen()) { RMinbox.chip(n); return true; }
    if (document.getElementById('replyText')) { chipOnPage(n); return true; }
    return false;
  }

  document.addEventListener('keydown', function (e) {
    // Alt/Option + digit works from inside the reply box, where plain digits are typing.
    if (e.altKey && !e.metaKey && !e.ctrlKey && /^Digit[1-9]$/.test(e.code || '')) {
      if (pickTemplate(parseInt(e.code.slice(5), 10))) e.preventDefault();
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'Escape') {
      if (help && !help.hidden) { closeHelp(); return; }
      if (window.RMinbox && RMinbox.isOpen()) { RMinbox.close(); return; }
      if (typing(e)) e.target.blur();
      return;
    }
    if (typing(e)) return;
    if (e.key === '?') { e.preventDefault(); toggleHelp(); return; }
    if (help && !help.hidden) return;
    if (e.key === '/') { var q = document.getElementById('q'); if (q) { e.preventDefault(); q.focus(); q.select(); } return; }
    if ((e.key === 'Enter' || e.key === ' ') && onControl(e)) return;   // let the focused control do its own thing
    var inbox = !!document.getElementById('inboxTable'), review = !!document.getElementById('replyText') || !!document.querySelector('.banner');
    if (inbox) {
      var r = current();
      if (e.key === 'j' || (e.key === 'ArrowDown' && sel >= 0)) { e.preventDefault(); select(sel + 1); }
      else if (e.key === 'k' || (e.key === 'ArrowUp' && sel >= 0)) { e.preventDefault(); select(sel - 1); }
      else if ((e.key === 'Enter' || e.key === 'o') && r) { location.href = r.dataset.href; }
      else if (e.key === 'r' && r) { e.preventDefault(); if (window.RMinbox) RMinbox.open(r.dataset.id); }
      else if (e.key === 'e' && r) { e.preventDefault(); if (window.RMinbox) RMinbox.archive(r.dataset.id); }
      else if (/^[1-9]$/.test(e.key) && window.RMinbox && RMinbox.isOpen()) { e.preventDefault(); RMinbox.chip(parseInt(e.key, 10)); }
    } else if (review) {
      if (e.key === 'r') { var ta = document.getElementById('replyText'); if (ta) { e.preventDefault(); ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); } }
      else if (e.key === 'e') { var f = document.getElementById('archiveForm'); if (f) { e.preventDefault(); f.requestSubmit(); } }
      else if (e.key === 'n') { var n = document.getElementById('navNext'); if (n) n.click(); }
      else if (e.key === 'p') { var p = document.getElementById('navPrev'); if (p) p.click(); }
      else if (/^[1-9]$/.test(e.key)) { e.preventDefault(); chipOnPage(parseInt(e.key, 10)); }
    }
  });
})();
