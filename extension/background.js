// Senim: пункт «Проверить в Senim» в меню правой кнопки для выделенного текста.
const DEFAULT_SERVER = "http://127.0.0.1:8000";
const MAX_CHARS = 12000;

async function getServer() {
  const { server } = await chrome.storage.sync.get({ server: DEFAULT_SERVER });
  return (server || DEFAULT_SERVER).replace(/\/+$/, "");
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
});
