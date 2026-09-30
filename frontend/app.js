/* Senim — фронтенд без сборки. Весь текст вставляется через textContent (без XSS). */
(() => {
  "use strict";

  // ------------------------------------------------------------ helpers
  const $ = (id) => document.getElementById(id);
  const STATUS_ORDER = { contradicted: 4, disputed: 3, supported: 2, unverifiable: 1 };
  const state = {
    lang: loadPref("senim.lang", "ru"),
    text: "", sentences: [], claims: new Map(), results: new Map(), citations: [], trust: null,
    demo: false, running: false, done: false,
    think: false, marked: new Set(), revealed: false,
    selectedSentence: null, examples: [],
  };

  function loadPref(k, d) { try { return localStorage.getItem(k) || d; } catch { return d; } }
  function savePref(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } }

  function t(key, vars) {
    const dict = window.I18N[state.lang] || window.I18N.ru;
    let s = dict[key] ?? window.I18N.ru[key] ?? key;
    if (vars) for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, v);
    return s;
  }

  function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  const show = (id, on = true) => $(id).classList.toggle("hidden", !on);

  // ------------------------------------------------------------ i18n
  function applyI18n() {
    document.documentElement.lang = state.lang;
    document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
    document.querySelectorAll(".lang-switch button").forEach((b) =>
      b.classList.toggle("active", b.dataset.lang === state.lang));
    $("question").placeholder = "…";
    renderExamples();
    renderAll();
    renderSkill();
    renderStats();
    refreshProvider();
  }

  // ------------------------------------------------------------ provider pill
  let health = null;
  async function refreshProvider() {
    try {
      if (!health) health = await (await fetch("/api/health")).json();
      const links = health.links || {};
      if (links.telegram_bot) { $("lnkBot").href = links.telegram_bot; $("lnkBot").classList.remove("hidden"); }
      if (links.extension_zip) { $("lnkExt").href = links.extension_zip; $("lnkExt").classList.remove("hidden"); $("extHint").classList.remove("hidden"); }
      $("channels").classList.toggle("hidden", !(links.telegram_bot || links.extension_zip));
      const pill = $("providerPill");
      if (health.llm_available) {
        pill.textContent = t("provider_ok") + health.llm_chain.split(" → ")[0];
        pill.className = "pill pill-ok";
        pill.title = health.llm_chain;
      } else {
        pill.textContent = t("provider_none");
        pill.className = "pill pill-warn";
        pill.title = ".env → LLM_PROVIDER + API key";
      }
    } catch { /* сервер недоступен */ }
  }

  // ------------------------------------------------------------ examples
  async function loadExamples() {
    try { state.examples = (await (await fetch("/api/examples")).json()).examples; } catch { state.examples = []; }
    renderExamples();
  }
  function renderExamples() {
    const box = $("exampleChips");
    box.replaceChildren(...state.examples.map((ex) =>
      h("button", { class: "chip", onclick: () => { $("answer").value = ex.text; $("question").value = ex.question || ""; updateCount(); } },
        ex.label[state.lang] || ex.label.ru)));
  }

  function updateCount() { $("charCount").textContent = `${$("answer").value.length} / 12000`; }

  // ------------------------------------------------------------ run
  function reset() {
    Object.assign(state, {
      sentences: [], claims: new Map(), results: new Map(), citations: [], trust: null,
      demo: false, done: false, marked: new Set(), revealed: false, selectedSentence: null, llmError: false,
    });
    $("notice").classList.add("hidden");
    ["summaryCard", "textCard", "claimsCard", "citationsCard", "thinkPanel", "feedbackCard"].forEach((id) => show(id, false));
    resetFeedback();
    $("thinkScore").classList.add("hidden");
  }

  function notice(msg, kind = "warn") {
    const n = $("notice");
    n.textContent = msg;
    n.className = `notice ${kind}`;
  }

  async function streamFrom(url, body) {
    state.running = true;
    setButtons();
    show("emptyState", false);
    show("progress", true);
    setProgress(t("progress_start"), 5);
    try {
      const res = await fetch(url, body ? {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      } : undefined);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        const d = err.detail;
        throw new Error(d && d.message ? d.message : d ? JSON.stringify(d) : `HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (line) handleEvent(JSON.parse(line));
        }
      }
      if (buf.trim()) handleEvent(JSON.parse(buf));
    } catch (e) {
      notice(t("err_generic") + e.message, "error");
    } finally {
      state.running = false;
      show("progress", false);
      setButtons();
    }
  }

  function handleEvent(ev) {
    switch (ev.type) {
      case "start":
        state.sentences = ev.sentences;
        state.demo = !!ev.demo;
        if (state.demo) notice(t("demo_badge"), "info");
        setProgress(t("progress_claims"), 15);
        show("textCard", true);
        if (state.think) { show("thinkPanel", true); $("thinkStatus").textContent = t("thinkWaiting"); }
        break;
      case "claims":
        ev.claims.forEach((c) => state.claims.set(c.id, c));
        setProgress(t("progress_check", { done: 0, total: ev.claims.length }), 25);
        break;
      case "claim_result":
        state.results.set(ev.result.claim_id, ev.result);
        setProgress(t("progress_check", { done: state.results.size, total: state.claims.size }),
          25 + 65 * (state.results.size / Math.max(1, state.claims.size)));
        if (state.results.size === state.claims.size) setProgress(t("progress_cit"), 92);
        break;
      case "citations":
        state.citations = ev.results;
        break;
      case "error":
        state.llmError = true;
        if (ev.code === "llm_not_configured") notice(t("err_llm"), "warn");
        else notice(t("err_generic") + ev.message, "error");
        break;
      case "done":
        state.trust = ev.trust;
        state.done = true;
        state.usage = ev.usage || null;
        if (!state.demo && state.claims.size) show("feedbackCard", true);
        state.elapsed = ev.elapsed_ms || 0;
        loadStats();
        if (state.think) {
          $("btnReveal").disabled = false;
          $("thinkStatus").textContent = t("thinkReady", { n: state.marked.size });
        }
        break;
    }
    renderAll();
  }

  function setProgress(text, pct) {
    $("progressText").textContent = text;
    $("progressBar").style.width = `${Math.min(100, pct)}%`;
  }

  function setButtons() {
    ["btnCheck", "btnCitations", "btnDemo"].forEach((id) => { $(id).disabled = state.running; });
  }

  // ------------------------------------------------------------ status per sentence
  function claimsForSentence(idx) {
    return [...state.claims.values()].filter((c) => c.sentence_index === idx);
  }

  function citationForSentence(s) {
    return state.citations.find((r) => r.citation.raw && s.text.includes(r.citation.raw.slice(0, 40)));
  }

  function sentenceStatus(s) {
    let worst = null;
    let pending = false;
    for (const c of claimsForSentence(s.index)) {
      const r = state.results.get(c.id);
      if (!r) { pending = true; continue; }
      if (!worst || STATUS_ORDER[r.status] > STATUS_ORDER[worst]) worst = r.status;
    }
    const cr = citationForSentence(s);
    if (cr) {
      const cs = cr.status === "doi_not_found" || cr.notes.includes("doi_other_work") ? "contradicted"
        : cr.status === "mismatch" ? "disputed" : cr.status === "verified" ? "supported" : "unverifiable";
      if (!worst || STATUS_ORDER[cs] > STATUS_ORDER[worst]) worst = cs;
    }
    return { status: worst, pending: pending && !worst };
  }

  const isProblem = (st) => st === "contradicted" || st === "disputed";

  // ------------------------------------------------------------ render
  function renderAll() {
    renderSummary();
    renderHighlighted();
    renderClaims();
    renderCitations();
  }

  function colorsVisible() { return !state.think || state.revealed; }

  function renderHighlighted() {
    if (!state.sentences.length) return;
    const box = $("highlighted");
    const nodes = [];
    state.sentences.forEach((s, i) => {
      const { status, pending } = sentenceStatus(s);
      const cls = ["sent"];
      if (colorsVisible()) {
        if (status) cls.push(`s-${status}`);
        else if (pending && state.running) cls.push("pending");
      }
      if (state.think) {
        if (state.marked.has(s.index)) cls.push("marked");
        if (state.revealed) {
          if (isProblem(status) && !state.marked.has(s.index)) cls.push("missed");
          if (!isProblem(status) && state.marked.has(s.index)) cls.push("false-alarm");
        }
      }
      if (state.selectedSentence === s.index) cls.push("selected");
      nodes.push(h("span", {
        class: cls.join(" "), "data-index": s.index, tabindex: 0, role: "button",
        onclick: () => onSentenceClick(s.index),
        onkeydown: (e) => { if (e.key === "Enter") onSentenceClick(s.index); },
      }, s.text));
      // сохраняем переносы строк между предложениями
      const next = state.sentences[i + 1];
      const between = next ? (state.textSource || "").slice(s.end, next.start) : "";
      nodes.push(between.includes("\n") ? h("br") : " ");
    });
    box.replaceChildren(...nodes);
  }

  function onSentenceClick(idx) {
    if (state.think && !state.revealed) {
      state.marked.has(idx) ? state.marked.delete(idx) : state.marked.add(idx);
      if (state.done) $("thinkStatus").textContent = t("thinkReady", { n: state.marked.size });
      renderHighlighted();
      return;
    }
    state.selectedSentence = state.selectedSentence === idx ? null : idx;
    renderHighlighted();
    renderClaims();
    const first = document.querySelector(".claim.focus");
    if (first) first.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function renderSummary() {
    const tr0 = state.trust;
    // ИИ не ответил и фактов нет — индекс не показываем, чтобы не вводить в заблуждение
    if (!tr0 || !colorsVisible() || (tr0.band === "na" && state.llmError && !tr0.fabricated_citations)) {
      show("summaryCard", false); return;
    }
    show("summaryCard", true);
    const tr = state.trust;
    const val = tr.band === "na" ? "–" : tr.trust_index;
    $("trustValue").textContent = val;
    const arc = $("gaugeArc");
    const C = 2 * Math.PI * 50;
    arc.style.strokeDasharray = `${C}`;
    arc.style.strokeDashoffset = `${C * (1 - (tr.band === "na" ? 0 : tr.trust_index) / 100)}`;
    arc.setAttribute("class", `gauge-arc band-${tr.band}`);
    const band = $("bandLabel");
    band.textContent = t(`band_${tr.band}`);
    band.className = `band band-${tr.band}`;
    const c = tr.counts;
    const n = (c.supported || 0) + (c.disputed || 0) + (c.contradicted || 0) + (c.unverifiable || 0) + (c.opinion || 0);
    let line = n === 0 ? "" : t("summary", { n, s: c.supported || 0, d: c.disputed || 0, c: c.contradicted || 0, u: c.unverifiable || 0, o: c.opinion || 0 });
    if (tr.fabricated_citations) line += (line ? " " : "") + t("fabricated", { f: tr.fabricated_citations });
    $("summaryLine").textContent = line;
    show("cappedNote", !!tr.capped);
    const u = state.usage;
    const meta = [];
    if (state.elapsed) meta.push(`${(state.elapsed / 1000).toFixed(1)} ${t("sec")}`);
    if (u && u.calls) meta.push(`≈ $${u.cost_usd < 0.001 ? u.cost_usd.toFixed(5) : u.cost_usd.toFixed(4)} · ${u.calls} ${t("llmCalls")}`);
    else if (u && u.cache_hits) meta.push(t("fromCache"));
    $("checkMeta").textContent = meta.join(" · ");
  }

  function statusBadge(status) {
    return h("span", { class: `badge s-${status}` }, h("i", { class: `dot s-${status}` }), t(`st_${status}`));
  }

  function sourceLabel(ev) {
    if (!ev) return "";
    if (ev.source.startsWith("senim_kb")) return `${t("kbSource")} — ${ev.title}`;
    if (ev.source.startsWith("wikipedia")) return `Wikipedia (${ev.source.split(":")[1]}) — ${ev.title}`;
    return ev.title || ev.url;
  }

  function renderClaims() {
    if (!state.claims.size) { show("claimsCard", false); return; }
    show("claimsCard", true);
    const list = [...state.claims.values()].sort((a, b) => a.id - b.id);
    $("claimsList").replaceChildren(...list.map((c) => {
      const r = state.results.get(c.id);
      const visible = colorsVisible() && r;
      const focus = state.selectedSentence != null && c.sentence_index === state.selectedSentence;
      const card = h("article", { class: `claim ${visible ? "s-" + r.status : "pending-card"} ${focus ? "focus" : ""}` });
      const head = h("div", { class: "claim-head" },
        visible ? statusBadge(r.status) : h("span", { class: "badge muted" }, state.running ? "…" : "—"),
        h("span", { class: "imp", title: t("importance") }, "●".repeat(c.importance) + "○".repeat(3 - c.importance)),
        visible && r.error_type && r.error_type !== "none" ? h("span", { class: "tag" }, t(`err_${r.error_type}`)) : null,
        ...(visible ? r.flags.map((f) => h("span", { class: "tag tag-warn" }, t(`flag_${f}`))) : []),
      );
      card.append(head, h("p", { class: "claim-text" }, c.text));
      if (visible) {
        if (r.explanation) card.append(h("p", { class: "explain" }, h("b", {}, t("why") + " "), r.explanation));
        if (r.quote && r.evidence) {
          const link = r.evidence.url.startsWith("http")
            ? h("a", { href: r.evidence.url, target: "_blank", rel: "noopener" }, sourceLabel(r.evidence))
            : h("span", {}, sourceLabel(r.evidence));
          card.append(h("blockquote", { class: "quote" },
            h("div", { class: "small muted" }, t("quote")),
            h("div", {}, "«", r.quote, "»"),
            h("div", { class: "small src" }, t("source"), ": ", link),
            r.quote_verified ? h("div", { class: "small verified" }, "✓ ", t("quoteVerified")) : null));
        }
        if (r.correction) card.append(h("p", { class: "correction" }, h("b", {}, t("correction") + ": "), r.correction));
        if (r.how_to_check) card.append(h("p", { class: "small howto" }, "💡 ", h("b", {}, t("howToCheck") + ": "), r.how_to_check));
      }
      card.addEventListener("click", () => {
        if (c.sentence_index != null && colorsVisible()) {
          state.selectedSentence = c.sentence_index;
          renderHighlighted();
          document.querySelector(`.sent[data-index="${c.sentence_index}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      });
      return card;
    }));
  }

  function renderCitations() {
    const shouldShow = state.citations.length || (state.done && state.claims.size === 0 && !state.sentences.length);
    if (!shouldShow || !colorsVisible()) { show("citationsCard", false); return; }
    show("citationsCard", true);
    if (!state.citations.length) { $("citationsList").replaceChildren(h("p", { class: "muted" }, t("no_citations"))); return; }
    $("citationsList").replaceChildren(...state.citations.map((r) => {
      const fabricated = r.status === "doi_not_found" || r.notes.includes("doi_other_work");
      const st = fabricated ? "contradicted" : r.status === "mismatch" ? "disputed" : r.status === "verified" ? "supported" : "unverifiable";
      const label = r.notes.includes("doi_other_work") ? t("note_doi_other_work") : t(`cit_${r.status}`);
      return h("article", { class: `citation s-${st}` },
        h("div", { class: "claim-head" }, h("span", { class: `badge s-${st}` }, h("i", { class: `dot s-${st}` }), label),
          r.citation.doi ? h("span", { class: "tag mono" }, "DOI " + r.citation.doi) : null),
        h("p", { class: "cit-raw" }, r.citation.raw),
        r.notes.length ? h("p", { class: "small muted" }, r.notes.map((n) => t(`note_${n}`)).join(" · ")) : null,
        r.matched_title ? h("p", { class: "small" }, h("b", {}, t("found_as") + ": "),
          r.matched_url ? h("a", { href: r.matched_url, target: "_blank", rel: "noopener" }, r.matched_title) : r.matched_title,
          r.matched_year ? ` (${r.matched_year})` : "",
          r.matched_authors?.length ? ` — ${r.matched_authors.slice(0, 3).join(", ")}` : "") : null,
        r.checked_in.length ? h("p", { class: "small muted" }, "✓ " + r.checked_in.join(", ")) : null,
      );
    }));
  }

  // ------------------------------------------------------------ think first
  function reveal() {
    state.revealed = true;
    const problems = state.sentences.filter((s) => isProblem(sentenceStatus(s).status)).map((s) => s.index);
    const found = problems.filter((i) => state.marked.has(i)).length;
    const falseAlarms = [...state.marked].filter((i) => !problems.includes(i)).length;
    const box = $("thinkScore");
    const parts = [];
    if (!problems.length) parts.push(h("p", {}, t("thinkNone")));
    else {
      parts.push(h("p", { class: "big" }, t("thinkResult", { found, total: problems.length })));
      parts.push(h("p", { class: "small" }, found === problems.length ? t("thinkPerfect") : t("thinkMissed")));
    }
    if (falseAlarms) parts.push(h("p", { class: "small muted" }, t("thinkFalse", { n: falseAlarms })));
    box.replaceChildren(...parts);
    box.classList.remove("hidden");
    $("btnReveal").disabled = true;
    if (state.cls && !state.demo) {  // итог «Сначала подумай» — в панель учителя
      fetch(`/api/class/${state.cls.code}/think`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ student: $("studentName").value.trim(), caught: found,
                               missed: problems.length - found, false_alarms: falseAlarms }),
      }).catch(() => {});
    }
    if (problems.length) {
      const f = Number(loadPref("senim.skill.found", "0")) + found;
      const tot = Number(loadPref("senim.skill.total", "0")) + problems.length;
      savePref("senim.skill.found", String(f));
      savePref("senim.skill.total", String(tot));
      renderSkill();
    }
    renderAll();
  }

  function renderSkill() {
    const f = loadPref("senim.skill.found", "0"), tot = loadPref("senim.skill.total", "0");
    $("skillCounter").textContent = Number(tot) ? t("skill", { found: f, total: tot }) : "";
  }

  // ------------------------------------------------------------ actions
  function currentText() { return $("answer").value.trim(); }

  function startCheck() {
    const text = currentText();
    if (text.length < 10) { $("answer").focus(); return; }
    reset();
    state.think = $("thinkFirst").checked;
    state.textSource = $("answer").value;
    // смещения предложений считаются по тексту после trim на сервере — используем тот же текст
    state.textSource = text;
    const body = { text, question: $("question").value.trim(), ui_lang: state.lang, source_ai: $("sourceAi").value };
    if (state.cls) {
      const name = $("studentName").value.trim();
      if (!name) { notice(t("classNeedName"), "warn"); $("studentName").focus(); return; }
      body.class_code = state.cls.code;
      body.student = name;
    }
    streamFrom(`/api/check/stream?channel=${encodeURIComponent(state.channel)}`, body);
  }

  async function checkCitationsOnly() {
    const text = currentText();
    if (text.length < 5) { $("answer").focus(); return; }
    reset();
    state.think = false;
    state.running = true; setButtons();
    show("emptyState", false); show("progress", true); setProgress(t("progress_cit"), 40);
    try {
      const res = await fetch("/api/citations", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error((data.detail && data.detail.message) || `HTTP ${res.status}`);
      state.citations = data.citations || [];
      state.done = true;
    } catch (e) {
      notice(t("err_generic") + e.message, "error");
    } finally {
      state.running = false; setButtons(); show("progress", false);
      show("citationsCard", true);
      renderCitations();
    }
  }

  function startDemo() {
    const ex = state.examples.find((e) => e.id === "abai_kk");
    if (ex) { $("answer").value = ex.text; $("question").value = ex.question; updateCount(); }
    reset();
    state.think = $("thinkFirst").checked;
    state.textSource = ex ? ex.text : "";
    streamFrom("/api/demo/abai_kk");
  }

  // ------------------------------------------------------------ init
  document.querySelectorAll(".lang-switch button").forEach((b) =>
    b.addEventListener("click", () => { state.lang = b.dataset.lang; savePref("senim.lang", state.lang); applyI18n(); }));
  $("btnCheck").addEventListener("click", startCheck);
  $("btnCitations").addEventListener("click", checkCitationsOnly);
  $("btnDemo").addEventListener("click", startDemo);
  $("btnReveal").addEventListener("click", reveal);
  $("answer").addEventListener("input", updateCount);
  $("answer").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) startCheck(); });

  // ---------------------------------------------------------- feedback (мини-опрос)
  function resetFeedback() {
    document.querySelectorAll(".fb-btns").forEach((g) => {
      g.classList.remove("done");
      g.querySelectorAll(".chip").forEach((b) => b.classList.remove("on"));
    });
    $("fbThanks").classList.add("hidden");
  }
  document.querySelectorAll(".fb-btns").forEach((group) => {
    group.addEventListener("click", async (e) => {
      const btn = e.target.closest(".chip");
      if (!btn || group.classList.contains("done")) return;
      group.classList.add("done");
      btn.classList.add("on");
      const q = group.dataset.q;
      const v = btn.dataset.v;
      const body = { lang: state.lang, channel: state.channel };
      body[q] = q === "useful" ? v === "true" : v;
      try {
        await fetch("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        $("fbThanks").classList.remove("hidden");
      } catch { /* ignore */ }
    });
  });

  // ---------------------------------------------------------- impact stats
  async function loadStats() {
    try {
      const st = await (await fetch("/api/stats")).json();
      state.stats = st;
      renderStats();
    } catch { /* ignore */ }
  }
  function renderStats() {
    const st = state.stats;
    if (!st || !st.checks) { $("impactLine").textContent = ""; return; }
    let line = t("impact", { checks: st.checks, errors: st.errors_caught, claims: st.claims_checked });
    if (st.avg_cost_per_check_usd) line += " · " + t("avgCost", { cost: st.avg_cost_per_check_usd.toFixed(4) });
    $("impactLine").textContent = line;
  }

  // ---------------------------------------------------------- launched from extension / link
  // /?text=...&src=extension — текст пришёл из расширения Chrome: сразу проверяем
  const params = new URLSearchParams(location.search);
  state.channel = ["extension", "telegram"].includes(params.get("src")) ? params.get("src") : "web";

  // ---------------------------------------------------------- режим учителя (класс по коду)
  function showClass() {
    const c = state.cls;
    show("classBar", !!c);
    if (c) { $("className").textContent = c.name; show("classJoin", false); }
  }
  async function joinClass(code, silent) {
    code = (code || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
    if (!code) return false;
    try {
      const res = await fetch(`/api/class/${code}`);
      if (!res.ok) throw new Error("404");
      const c = await res.json();
      state.cls = { code: c.code, name: c.name };
      savePref("senim.class", JSON.stringify(state.cls));
      showClass();
      if (!$("studentName").value) $("studentName").focus();
      return true;
    } catch {
      if (!silent) notice(t("classNotFound"), "warn");
      return false;
    }
  }
  try { state.cls = JSON.parse(loadPref("senim.class", "null")); } catch { state.cls = null; }
  $("studentName").value = loadPref("senim.student", "");
  $("studentName").addEventListener("change", () => savePref("senim.student", $("studentName").value.trim()));
  $("btnClassLeave").addEventListener("click", () => {
    state.cls = null; savePref("senim.class", "null"); showClass();
  });
  $("btnShowJoin").addEventListener("click", () => { show("classJoin", true); $("classCode").focus(); });
  $("btnClassJoin").addEventListener("click", () => joinClass($("classCode").value));
  $("classCode").addEventListener("keydown", (e) => { if (e.key === "Enter") joinClass($("classCode").value); });
  showClass();
  // чей ответ проверяем — запоминаем выбор; из расширения приходит автоматически (?ai=chatgpt)
  $("sourceAi").value = params.get("ai") || loadPref("senim.sourceAi", "");
  $("sourceAi").addEventListener("change", () => savePref("senim.sourceAi", $("sourceAi").value));
  if (params.get("class")) {
    joinClass(params.get("class"));
    if (!params.get("text")) history.replaceState(null, "", location.pathname);
  }

  applyI18n();
  loadExamples();
  loadStats();
  if (params.get("text")) {
    $("answer").value = params.get("text").slice(0, 12000);
    updateCount();
    history.replaceState(null, "", location.pathname);
    setTimeout(startCheck, 300);
  }
})();
