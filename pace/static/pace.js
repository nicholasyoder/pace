// Copy buttons: <button data-copy="#selector">. navigator.clipboard needs a
// secure context, which plain http over Tailscale isn't, so fall back to
// selecting the text and execCommand.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const el = document.querySelector(btn.dataset.copy);
  const done = () => { const t = btn.textContent; btn.textContent = "Copied"; setTimeout(() => (btn.textContent = t), 1500); };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(el.value).then(done);
  } else {
    el.select();
    if (document.execCommand("copy")) done();
  }
});

// Claude usage indicator (#usage): a <details> that opens on click, and on
// hover where there is one. refreshUsage() swaps in a fresh copy from /usage,
// on a "usage" SSE message and whenever the page is shown again (a tab coming
// back, or a page restored from the back/forward cache).
function usageDetails() { return document.querySelector("#usage details"); }
let usagePinned = false;
let usageTimer = null;
function refreshUsage() {
  const box = document.getElementById("usage");
  if (!box) return;
  fetch("/usage", { cache: "no-store" }).then((r) => (r.ok ? r.text() : null)).then((html) => {
    if (html === null) return;
    const wasOpen = usageDetails() && usageDetails().open;
    box.innerHTML = html;
    if (wasOpen && usageDetails()) usageDetails().open = true;
  }).catch(() => {});
}
function refreshUsageSoon() { clearTimeout(usageTimer); usageTimer = setTimeout(refreshUsage, 200); }
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshUsage(); });
window.addEventListener("pageshow", (e) => { if (e.persisted) refreshUsage(); });
(function () {
  const box = document.getElementById("usage");
  if (!box) return;
  const hover = window.matchMedia("(hover: hover)").matches;
  if (hover) {
    box.addEventListener("mouseenter", () => { const d = usageDetails(); if (d) d.open = true; });
    box.addEventListener("mouseleave", () => { const d = usageDetails(); if (d && !usagePinned) d.open = false; });
  }
  box.addEventListener("click", (e) => {
    if (!e.target.closest("summary")) return;
    if (hover) {  // hovering already opened it: a click pins it open, or unpins and closes
      e.preventDefault();
      usagePinned = !usagePinned;
      usageDetails().open = usagePinned;
    }
  });
  const close = () => { usagePinned = false; const d = usageDetails(); if (d) d.open = false; };
  document.addEventListener("click", (e) => { if (!e.target.closest("#usage")) close(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
})();

// Live updates over SSE. An element with data-sse names the stream:
// - "stream" messages append to #live-lines (the current run's activity);
// - "usage" messages refresh the usage indicator in place;
// - anything else means the page is out of date: reload, unless I've started
//   typing into a form, in which case show the "page has changed" banner.
(function () {
  const cfg = document.querySelector("[data-sse]");
  if (!cfg) return;
  let dirty = false;
  document.addEventListener("input", (e) => { if (e.target.closest("form")) dirty = true; });
  document.addEventListener("change", (e) => { if (e.target.closest("form")) dirty = true; });

  let timer = null;
  function connect() {
    const es = new EventSource(cfg.dataset.sse);
    es.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === "stream") {
        const pane = document.getElementById("live-lines");
        if (!pane) return;
        const stick = pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 20;
        const div = document.createElement("div");
        div.textContent = m.line;
        pane.appendChild(div);
        if (stick) pane.scrollTop = pane.scrollHeight;
        return;
      }
      if (m.type === "usage") { refreshUsageSoon(); return; }
      if (dirty) { document.getElementById("stale").hidden = false; return; }
      clearTimeout(timer);
      timer = setTimeout(() => location.reload(), 300);
    };
    es.onerror = () => { es.close(); setTimeout(connect, 5000); };
  }
  connect();

  const pane = document.getElementById("live-lines");
  if (pane) pane.scrollTop = pane.scrollHeight;
})();
