const DEFAULT_SERVER = "https://senim-616q.onrender.com";
const $ = (id) => document.getElementById(id);

const LOCAL = /^https?:\/\/(127\.0\.0\.1|localhost)(:|\/|$)/i;
chrome.storage.sync.get({ server: DEFAULT_SERVER }, ({ server }) => {
  if (!server || (LOCAL.test(server) && !LOCAL.test(DEFAULT_SERVER))) {
    server = DEFAULT_SERVER; chrome.storage.sync.set({ server });
  }
  $("server").value = server;
});
$("server").addEventListener("change", () => {
  const v = $("server").value.trim() || DEFAULT_SERVER;
  chrome.storage.sync.set({ server: v });
});

function open(text) {
  chrome.runtime.sendMessage({ type: "senim-open", text });
  window.close();
}

$("checkSel").addEventListener("click", async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => window.getSelection().toString(),
    });
    const text = (res && res.result || "").trim();
    if (text.length < 10) { $("msg").textContent = "Сначала выделите ответ ИИ на странице."; return; }
    open(text);
  } catch (e) {
    $("msg").textContent = "На этой странице нельзя прочитать выделение — вставьте текст в поле.";
  }
});

$("checkText").addEventListener("click", () => {
  const text = $("text").value.trim();
  if (text.length < 10) { $("msg").textContent = "Вставьте ответ ИИ (от 10 символов)."; return; }
  open(text);
});
