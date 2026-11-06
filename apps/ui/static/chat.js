"use strict";

const $ = id => document.getElementById(id);
const storageKey = "shams.webChat.v1";
const deletedPrefix = storageKey + ".deleted.";
const mobile = matchMedia("(max-width: 700px)");
const terminal = new Set(["ACCEPTED", "FAILED", "BLOCKED", "HUMAN_REVIEW", "CANCELED"]);
const inflight = new Set();
const drafts = new WeakMap();
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
let threads = [];
let active = null;
let models = [];
let modelError = "";
let saveError = "";
let pendingDelete = null;

function liveThreads(list) {
  return list.filter(thread => !localStorage.getItem(deletedPrefix + thread.id));
}

function savedHistory() {
  const raw = localStorage.getItem(storageKey);
  try {
    const saved = JSON.parse(raw);
    return Array.isArray(saved?.threads) ? saved : {};
  } catch (_) {
    return {};
  }
}

try {
  const saved = savedHistory();
  threads = liveThreads(saved.threads || []);
  active = threads.find(thread => thread.id === saved.active) || null;
} catch (_) {
  // A clean session is usable even when browser storage is unavailable.
}

function save(thread) {
  try {
    const stored = savedHistory().threads || [];
    if (thread && !localStorage.getItem(deletedPrefix + thread.id)) {
      const index = stored.findIndex(item => item.id === thread.id);
      if (index < 0) stored.unshift(thread);
      else stored[index] = thread;
    }
    const history = liveThreads(stored);
    localStorage.setItem(storageKey, JSON.stringify({ threads: history, active: active?.id }));
    saveError = "";
  } catch (_) {
    saveError = "Trình duyệt không thể lưu thêm lịch sử.";
  }
}

const entities = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => entities[c]);

function inline(text) {
  // Escape each literal segment and each token before adding markup.
  const tokens = /`([^`]+)`|\*\*([^*\n]+)\*\*|!?\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|<(https?:\/\/[^\s<>]+)>|(https?:\/\/[^\s<>()]+)|\*([^*\n]+)\*/g;
  let html = "", offset = 0;
  for (const match of text.matchAll(tokens)) {
    html += esc(text.slice(offset, match.index));
    if (match[1] !== undefined) html += "<code>" + esc(match[1]) + "</code>";
    else if (match[2] !== undefined) html += "<strong>" + esc(match[2]) + "</strong>";
    else if (match[7] !== undefined) html += "<em>" + esc(match[7]) + "</em>";
    else {
      const url = match[4] || match[5] || match[6].replace(/[.,;!?]+$/, "");
      const suffix = match[6] ? match[6].slice(url.length) : "";
      html += '<a target="_blank" rel="noopener noreferrer" href="' + esc(url) + '">' + esc(match[3] || url) + "</a>" + esc(suffix);
    }
    offset = match.index + match[0].length;
  }
  return (html + esc(text.slice(offset))).replace(/\n/g, "<br>");
}

function markdown(text) {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  let html = "", paragraph = [], list = [];
  let listType = "ul";
  function flush() {
    if (paragraph.length) html += "<p>" + inline(paragraph.join("\n")) + "</p>";
    if (list.length) html += "<" + listType + ">" + list.map(item => "<li>" + inline(item) + "</li>").join("") + "</" + listType + ">";
    paragraph = [];
    list = [];
  }
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = line.match(/^\s*```\s*([\w#+.-]*)/);
    const heading = line.match(/^(#{1,4})\s+(.*)/);
    const item = line.match(/^\s*([-*]|\d+[.)])\s+(.*)/);
    if (fence) {
      flush();
      const code = [];
      while (++i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i]);
      html += '<div class="codebox"><div class="code-head"><span>' + esc(fence[1] || "code") +
        '</span><button type="button" class="copy">Sao chép</button></div><pre><code>' +
        esc(code.join("\n")) + '</code></pre></div>';
    } else if (heading) {
      flush();
      const tag = "h" + heading[1].length;
      html += "<" + tag + ">" + inline(heading[2]) + "</" + tag + ">";
    } else if (item) {
      const type = /^\d/.test(item[1]) ? "ol" : "ul";
      if (paragraph.length || type !== listType) flush();
      listType = type;
      list.push(item[2]);
    } else if (/^>\s?/.test(line)) {
      flush();
      html += "<blockquote>" + inline(line.replace(/^>\s?/, "")) + "</blockquote>";
    } else if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || "")) {
      flush();
      const row = (source, tag) => "<tr>" + source.trim().slice(1, -1).split("|")
        .map(cell => "<" + tag + ">" + inline(cell.trim()) + "</" + tag + ">").join("") + "</tr>";
      html += '<div class="table-wrap"><table><thead>' + row(line, "th") + "</thead><tbody>";
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) html += row(lines[i++], "td");
      i--;
      html += "</tbody></table></div>";
    } else if (!line.trim()) {
      flush();
    } else {
      if (list.length) flush();
      paragraph.push(line);
    }
  }
  flush();
  return html;
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
  };
  const response = await fetch(path, options);
  if (response.ok) return response.json();
  const error = await response.json().catch(() => null);
  throw new Error(typeof error?.detail === "string" ? error.detail : "Yêu cầu thất bại (" + response.status + ").");
}

function sidebar(open) {
  $("shell").classList.toggle("closed", !open);
  $("open-sidebar").setAttribute("aria-expanded", String(open));
}

function syncComposer() {
  const prompt = $("prompt");
  prompt.style.height = "auto";
  prompt.style.height = Math.min(prompt.scrollHeight, 150) + "px";
  $("send").disabled = !prompt.value.trim() || $("model").disabled || inflight.has(active?.id);
  $("status").textContent = modelError || saveError;
  $("status").classList.toggle("error", Boolean(modelError || saveError));
}

function renderHistory() {
  $("history").replaceChildren();
  for (const thread of threads) {
    const row = document.createElement("div");
    row.className = "thread-row";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "thread";
    button.setAttribute("aria-current", String(thread === active));
    button.textContent = thread.title;
    button.title = thread.title;
    button.onclick = () => selectThread(thread);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "icon delete-thread";
    remove.title = "Xóa cuộc trò chuyện";
    remove.setAttribute("aria-label", "Xóa cuộc trò chuyện: " + thread.title);
    remove.innerHTML = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M4 7h16m-11 0V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5"/></svg>';
    remove.onclick = () => {
      pendingDelete = thread;
      $("delete-description").textContent = '“' + thread.title + '” sẽ bị xóa khỏi lịch sử trên trình duyệt này.' +
        (inflight.has(thread.id) ? " Lượt đang chạy vẫn tiếp tục trên máy chủ." : "");
      $("delete-dialog").returnValue = "cancel";
      $("delete-dialog").showModal();
    };
    row.append(button, remove);
    $("history").append(row);
  }
  if (!threads.length) $("history").innerHTML = '<div class="history-empty">Chưa có cuộc trò chuyện.</div>';
}

function discardThread(thread) {
  threads = threads.filter(item => item !== thread);
  inflight.delete(thread.id);
  if (active === thread) {
    active = null;
    $("prompt").value = "";
    renderConversation(true);
  }
  renderHistory();
}

$("delete-dialog").onclose = () => {
  pendingDelete = null;
};

$("delete-dialog").querySelector("form").onsubmit = event => {
  if (event.submitter?.value !== "delete") return;
  event.preventDefault();
  const thread = pendingDelete;
  if (!threads.includes(thread)) {
    $("delete-dialog").close();
    return;
  }
  let saved;
  try {
    saved = savedHistory();
    // Keep the deletion marker separate so stale tabs cannot restore the conversation.
    localStorage.setItem(deletedPrefix + thread.id, "1");
  } catch (_) {
    saveError = "Trình duyệt không lưu được thao tác xóa.";
    syncComposer();
    $("delete-dialog").close();
    return;
  }
  try {
    localStorage.setItem(storageKey, JSON.stringify({
      threads: liveThreads(saved.threads || []), active: saved.active === thread.id ? null : saved.active
    }));
    saveError = "";
  } catch (_) {
    saveError = "Đã ẩn cuộc trò chuyện nhưng trình duyệt chưa dọn được bản lưu cũ.";
  }
  discardThread(thread);
  syncComposer();
  $("delete-dialog").close();
  $("new-chat").focus();
};

addEventListener("storage", event => {
  if (!event.key?.startsWith(deletedPrefix) || !event.newValue) return;
  const thread = threads.find(item => item.id === event.key.slice(deletedPrefix.length));
  if (thread) discardThread(thread);
});

function renderConversation(scrollToEnd = false) {
  const scroll = $("scroll");
  const nearBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 60;
  const turns = active?.turns || [];
  const empty = !turns.length;
  $("main").classList.toggle("welcome", empty);
  $("welcome").hidden = !empty;
  $("suggestions").hidden = !empty;
  $("conversation").innerHTML = turns.map(turn => {
    return '<div class="user-message">' + esc(turn.prompt) + '</div><article class="reply">' +
      '<div class="reply-label"><img src="/static/logo.png" alt="">SHAMS</div><div class="reply-body">' +
      replyBody(turn) + "</div></article>";
  }).join("");
  if (scrollToEnd || nearBottom) scroll.scrollTop = scroll.scrollHeight;
  syncComposer();
}

function replyBody(turn) {
  if (turn.reply !== undefined) {
    const sources = (turn.sources || []).filter(url => /^https?:\/\/[^\s]+$/.test(url));
    const links = sources.length ? '<details class="sources"><summary>Trang SHAMS đã đọc (' + sources.length + ')</summary><ul>' +
      sources.map(url => '<li><a target="_blank" rel="noopener noreferrer" href="' + esc(url) + '">' + esc(url) + '</a></li>').join("") + '</ul></details>' : '';
    return '<div class="md">' + markdown(turn.reply) + '</div>' + links;
  }
  if (turn.error) return '<div class="error">' + esc(turn.error) + '</div>';
  const draft = drafts.get(turn);
  if (draft) return '<div class="md reply-live" aria-live="off">' + markdown(draft) + '</div>';
  return '<div class="reply-status">Đang chờ phản hồi…</div>';
}

function updateDraft(thread, turn, text) {
  if ((drafts.get(turn) || "") === text) return;
  drafts.set(turn, text);
  if (active !== thread) return;
  const body = $("conversation").querySelector(".reply:last-child .reply-body");
  const scroll = $("scroll");
  const nearBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 60;
  if (body) body.innerHTML = replyBody(turn);
  if (nearBottom) scroll.scrollTop = scroll.scrollHeight;
}

function selectThread(thread) {
  active = thread;
  if (thread && models.some(model => model.id === thread.model && model.status === "ok")) {
    $("model").value = thread.model;
  }
  $("prompt").value = "";
  if (mobile.matches) sidebar(false);
  save();
  renderHistory();
  renderConversation(true);
  if (!mobile.matches) $("prompt").focus();
}

function context(turns) {
  const history = [];
  let length = 0;
  for (const turn of turns.slice().reverse()) {
    if (turn.reply === undefined) continue;
    const size = turn.prompt.length + turn.reply.length;
    if (length + size > 100000 || history.length === 20) break;
    history.unshift({ role: "user", content: turn.prompt }, { role: "assistant", content: turn.reply });
    length += size;
  }
  return history;
}

async function watch(thread, turn, animateFinal = false) {
  try {
    while (threads.includes(thread)) {
      const task = await api("/api/web-chat/tasks/" + encodeURIComponent(turn.taskId));
      if (!threads.includes(thread)) return;
      if (!task.running) {
        const output = task.outputs.find(item => ["text/plain", "text/markdown"].includes(item.media_type));
        if (task.state === "ACCEPTED" && output) {
          const response = await fetch(output.url);
          if (!response.ok) throw new Error("Không đọc được câu trả lời.");
          const reply = await response.text();
          // Providers without live text still get a brief reveal for newly sent messages.
          if (animateFinal && !drafts.get(turn) && !reducedMotion.matches && active === thread) {
            const chars = Array.from(reply);
            const step = Math.max(4, Math.ceil(chars.length / 24));
            for (let i = step; i < chars.length && active === thread; i += step) {
              updateDraft(thread, turn, chars.slice(0, i).join(""));
              await new Promise(resolve => setTimeout(resolve, 28));
            }
          }
          turn.reply = reply;
          turn.sources = task.sources;
        } else {
          turn.error = task.error || (terminal.has(task.state) ? "Model chưa trả về văn bản (" + task.state + ")." :
            "Lượt chat bị gián đoạn. Hãy gửi lại tin nhắn.");
        }
        break;
      }
      if (typeof task.partial === "string") updateDraft(thread, turn, task.partial);
      await new Promise(resolve => setTimeout(resolve, 150));
    }
  } catch (error) {
    turn.error = error.message;
  } finally {
    drafts.delete(turn);
    inflight.delete(thread.id);
    if (threads.includes(thread)) save(thread);
    if (active === thread) renderConversation();
  }
}

$("composer").onsubmit = async event => {
  event.preventDefault();
  if ($("send").disabled) return;
  const prompt = $("prompt").value.trim();
  const preset = $("model").value;
  const thread = active || { id: crypto.randomUUID(), title: prompt.replace(/\s+/g, " ").slice(0, 70), turns: [] };
  if (!active) threads.unshift(thread);
  active = thread;
  thread.model = preset;
  const history = context(thread.turns);
  const turn = { prompt };
  thread.turns.push(turn);
  inflight.add(thread.id);
  $("prompt").value = "";
  renderHistory();
  renderConversation(true);
  try {
    const task = await api("/api/web-chat/send", { preset, prompt, history });
    if (!threads.includes(thread)) return;
    turn.taskId = task.task_id;
    save(thread);
    await watch(thread, turn, true);
  } catch (error) {
    turn.error = error.message;
    inflight.delete(thread.id);
    if (threads.includes(thread)) save(thread);
    if (active === thread) renderConversation();
  }
};

$("prompt").oninput = syncComposer;
$("prompt").onkeydown = event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
};
$("open-sidebar").onclick = () => { sidebar(true); $("close-sidebar").focus(); };
$("close-sidebar").onclick = $("scrim").onclick = () => { sidebar(false); $("open-sidebar").focus(); };
$("new-chat").onclick = () => selectThread(null);
mobile.addEventListener("change", () => sidebar(!mobile.matches));
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !$("delete-dialog").open && mobile.matches &&
      !$("shell").classList.contains("closed")) $("scrim").click();
});
$("suggestions").onclick = event => {
  if (!event.target.matches(".suggestion")) return;
  $("prompt").value = event.target.textContent + " ";
  $("prompt").focus();
  syncComposer();
};
$("conversation").onclick = async event => {
  const button = event.target.closest(".copy");
  if (!button) return;
  try {
    await navigator.clipboard.writeText(button.closest(".codebox").querySelector("code").textContent);
    button.textContent = "Đã chép";
  } catch (_) {
    button.textContent = "Không chép được";
  }
  setTimeout(() => { button.textContent = "Sao chép"; }, 1500);
};

async function loadModels() {
  try {
    ({ models } = await api("/api/web-chat/models"));
    const available = models.filter(model => model.status === "ok");
    $("model").replaceChildren();
    for (const model of available) {
      $("model").add(new Option(model.model || model.label, model.id));
    }
    if (!available.length) {
      $("model").add(new Option("Chưa có model chat khả dụng"));
      modelError = "Kiểm tra Ollama và các model đã cấu hình trong SHAMS.";
    } else {
      $("model").disabled = false;
      if (available.some(model => model.id === active?.model)) $("model").value = active.model;
    }
  } catch (error) {
    $("model").replaceChildren(new Option("Không tải được model"));
    modelError = error.message + " Hãy tải lại trang để thử lại.";
  }
  syncComposer();
}

sidebar(!mobile.matches);
for (const thread of threads) {
  const pending = thread.turns.find(turn => turn.taskId && turn.reply === undefined && !turn.error);
  if (pending) {
    inflight.add(thread.id);
    watch(thread, pending);
  }
}
renderHistory();
renderConversation(true);
loadModels();
