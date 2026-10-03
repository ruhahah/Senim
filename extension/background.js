// Senim: пункт «Проверить в Senim» в меню правой кнопки для выделенного текста.
const DEFAULT_SERVER = "https://senim-616q.onrender.com";
const MAX_CHARS = 12000;
const LOCAL = /^https?:\/\/(127\.0\.0\.1|localhost)(:|\/|$)/i;

async function getServer() {
  let { server } = await chrome.storage.sync.get({ server: DEFAULT_SERVER });
  // старая сохранённая настройка «локалхост» не должна перебивать адрес опубликованного сайта
  if (!server || (LOCAL.test(server) && !LOCAL.test(DEFAULT_SERVER))) server = DEFAULT_SERVER;
  return server.replace(/\/+$/, "");
}

async function openSenim(text) {
  const server = await getServer();
  const clean = (text || "").trim().slice(0, MAX_CHARS);
  const url = clean
    ? `${server}/?src=extension&text=${encodeURIComponent(clean)}`
    : `${server}/?src=extension`;
  await chrome.tabs.create({ url });
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "senim-check",
    title: "Проверить в Senim / Senim-де тексеру",
    contexts: ["selection"],
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "senim-check") return;
  // selectionText теряет переносы строк — пробуем взять выделение со страницы целиком
  let text = info.selectionText || "";
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => window.getSelection().toString(),
    });
    if (res && res.result && res.result.length >= text.length) text = res.result;
  } catch (e) { /* страница не даёт читать выделение — используем selectionText */ }
  openSenim(text);
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "senim-open") openSenim(msg.text);
  if (msg && msg.type === "senim-upload") {
    getServer().then((server) => chrome.tabs.create({ url: `${server}/?src=extension&upload=1` }));
  }
});


// ---- проверка прямо со страницы ChatGPT / Gemini (content.js): запрос к API Senim из фона
async function checkInline(text, ai) {
  const server = await getServer();
  const { classCode, student } = await chrome.storage.sync.get({ classCode: "", student: "" });
  const body = { text: text.slice(0, MAX_CHARS), source_ai: ai || "" };
  if (classCode && student) { body.class_code = classCode; body.student = student; }
  const fullUrl = `${server}/?src=extension&ai=${encodeURIComponent(ai || "")}&text=${encodeURIComponent(text.slice(0, 1800))}`;
  try {
    const res = await fetch(`${server}/api/check?channel=extension`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const d = data.detail;
      return { ok: false, error: (d && d.message) || (typeof d === "string" ? d : `HTTP ${res.status}`), fullUrl };
    }
    return { ok: true, data, fullUrl };
  } catch (e) {
    return { ok: false, error: String(e && e.message || e), fullUrl };
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "senim-check") {
    checkInline(msg.text || "", msg.ai).then(sendResponse);
    return true; // ответ придёт асинхронно
  }
});
