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

// Live updates over SSE. An element with data-sse names the stream:
// - "stream" messages append to #live-lines (the current run's activity);
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
