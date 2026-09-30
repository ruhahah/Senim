// Senim прямо в ChatGPT / Gemini: кнопка «Проверить в Senim» под каждым ответом ИИ,
// подсветка утверждений цветом прямо в тексте ответа (CSS Custom Highlight API — страницу не меняем)
// и короткий разбор под ответом.
(() => {
  "use strict";
  if (window.__senimInjected) return;
  window.__senimInjected = true;

  const SITES = [
    { test: /(^|\.)chatgpt\.com$|(^|\.)chat\.openai\.com$/, ai: "chatgpt", sel: '[data-message-author-role="assistant"]' },
    { test: /(^|\.)gemini\.google\.com$/, ai: "gemini", sel: "message-content, .model-response-text" },
    { test: /^(localhost|127\.0\.0\.1)$/, ai: "chatgpt", sel: '[data-message-author-role="assistant"]' }, // для тестов
  ];
  const site = SITES.find((s) => s.test.test(location.hostname));
  if (!site) return;

  const L = (() => {
    const l = (navigator.language || "ru").slice(0, 2);
    return l === "kk" ? "kk" : l === "en" ? "en" : "ru";
  })();
  const T = {
    ru: { btn: "Проверить в Senim", wait: "Senim проверяет…", full: "Полный разбор →", why: "Почему", fix: "Верно",
          band: { high: "Можно опираться", medium: "Проверяй внимательно", low: "Не используй без проверки", na: "Проверяемых фактов нет" },
          ok: "Ошибок не найдено", err: "Не удалось проверить: ", again: "Проверить снова" },
    kk: { btn: "Senim-де тексеру", wait: "Senim тексеріп жатыр…", full: "Толық талдау →", why: "Неге", fix: "Дұрысы",
          band: { high: "Сүйенуге болады", medium: "Мұқият тексеріңіз", low: "Тексермей қолданбаңыз", na: "Тексерілетін факт жоқ" },
          ok: "Қате табылмады", err: "Тексеру мүмкін болмады: ", again: "Қайта тексеру" },
    en: { btn: "Check in Senim", wait: "Senim is checking…", full: "Full report →", why: "Why", fix: "Correct",
          band: { high: "Safe to rely on", medium: "Check carefully", low: "Don't use without checking", na: "No checkable facts" },
          ok: "No errors found", err: "Couldn't check: ", again: "Check again" },
  }[L];

  const HL = ["contradicted", "disputed", "supported"];
  const ranges = { contradicted: [], disputed: [], supported: [] };
  const hasHighlights = typeof CSS !== "undefined" && CSS.highlights && typeof Highlight !== "undefined";

  function paint() {
    if (!hasHighlights) return;
    for (const k of HL) CSS.highlights.set(`senim-${k}`, new Highlight(...ranges[k]));
  }

  // ---- поиск предложения в DOM ответа: собираем текстовые узлы и их смещения
  function textIndex(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.parentElement && n.parentElement.closest(".senim-ui") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    const nodes = []; let full = "";
    for (let n = walker.nextNode(); n; n = walker.nextNode()) { nodes.push({ node: n, start: full.length }); full += n.nodeValue; }
    return { nodes, full };
  }
  const norm = (s) => s.replace(/\s+/g, " ").trim();

  function locate(idx, needle) {
    // ищем без учёта пробелов/переносов: строим «сжатую» строку и карту позиций
    const map = []; let squashed = ""; let prevSpace = false;
    for (let i = 0; i < idx.full.length; i++) {
      const ch = idx.full[i];
      if (/\s/.test(ch)) { if (!prevSpace && squashed) { squashed += " "; map.push(i); } prevSpace = true; }
      else { squashed += ch; map.push(i); prevSpace = false; }
    }
    let n = norm(needle);
    let at = squashed.indexOf(n);
    if (at < 0 && n.length > 50) { n = n.slice(0, 50); at = squashed.indexOf(n); }
    if (at < 0) return null;
    return [map[at], map[at + n.length - 1] + 1];
  }

  function toRange(idx, a, b) {
    const pos = (p) => {
      for (let i = idx.nodes.length - 1; i >= 0; i--) if (idx.nodes[i].start <= p) return [idx.nodes[i].node, p - idx.nodes[i].start];
      return [idx.nodes[0].node, 0];
    };
    const r = document.createRange();
    const [sn, so] = pos(a); const [en, eo] = pos(b - 1);
    r.setStart(sn, so); r.setEnd(en, Math.min(eo + 1, en.nodeValue.length));
    return r;
  }

  function el(tag, cls, ...kids) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    for (const k of kids) e.append(k instanceof Node ? k : document.createTextNode(k == null ? "" : String(k)));
    return e;
  }

  async function check(msgEl, box) {
    const text = (msgEl.innerText || "").trim();
    if (text.length < 10) return;
    box.replaceChildren(el("div", "senim-wait", el("span", "senim-spin"), T.wait));
    const res = await chrome.runtime.sendMessage({ type: "senim-check", text: text.slice(0, 12000), ai: site.ai });
    if (!res || !res.ok) {
      const retry = el("button", "senim-btn", T.again); retry.onclick = () => check(msgEl, box);
      box.replaceChildren(el("div", "senim-err", T.err + ((res && res.error) || "?")), retry);
      return;
    }
    render(msgEl, box, res.data, res.fullUrl);
  }

  function render(msgEl, box, out, fullUrl) {
    // подсветка в тексте ответа
    const idx = textIndex(msgEl);
    const claims = new Map((out.claims || []).map((c) => [c.id, c]));
    const seen = new Set();
    for (const r of out.results || []) {
      const c = claims.get(r.claim_id);
      if (!c || !HL.includes(r.status) || !idx.nodes.length) continue;
      const span = c.span || c.text;
      const key = `${r.status}|${span}`;
      if (seen.has(span) && r.status === "supported") continue;
      seen.add(span); seen.add(key);
      const at = locate(idx, span);
      if (at) ranges[r.status].push(toRange(idx, at[0], at[1]));
    }
    paint();

    // карточка разбора под ответом
    const tr = out.trust || { band: "na", trust_index: 0 };
    const head = el("div", `senim-head band-${tr.band}`,
      el("b", null, tr.band === "na" ? "–" : String(tr.trust_index)), el("span", null, T.band[tr.band] || ""));
    const list = el("div", "senim-list");
    const problems = (out.results || []).filter((r) => r.status === "contradicted" || r.status === "disputed");
    for (const r of problems) {
      const c = claims.get(r.claim_id);
      if (!c) continue;
      list.append(el("div", `senim-item s-${r.status}`,
        el("div", "senim-claim", c.text),
        r.explanation ? el("div", "senim-why", `${T.why}: ${r.explanation}`) : "",
        r.correction ? el("div", "senim-why", `${T.fix}: ${r.correction}`) : ""));
    }
    if (!problems.length) list.append(el("div", "senim-ok", "✓ " + T.ok));
    const link = el("a", "senim-link", T.full); link.href = fullUrl; link.target = "_blank"; link.rel = "noopener";
    box.replaceChildren(head, list, link);
  }

  function attach(msgEl) {
    if (msgEl.dataset.senim) return;
    msgEl.dataset.senim = "1";
    const box = el("div", "senim-ui");
    const btn = el("button", "senim-btn", el("span", "senim-dot"), T.btn);
    btn.onclick = () => check(msgEl, box);
    box.append(btn);
    msgEl.after(box);
  }

  // ответы появляются постепенно (стриминг) — кнопку ставим, когда текст перестал меняться
  const pending = new Map();
  function scan() {
    document.querySelectorAll(site.sel).forEach((m) => {
      if (m.dataset.senim) return;
      const len = (m.innerText || "").length;
      const prev = pending.get(m);
      if (prev !== undefined && prev === len && len > 20) { pending.delete(m); attach(m); }
      else pending.set(m, len);
    });
  }
  setInterval(scan, 1200);
  scan();
})();
