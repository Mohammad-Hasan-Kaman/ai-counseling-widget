(function () {
  "use strict";

  // API key from the iframe query string
  var params = new URLSearchParams(window.location.search);
  var apiKey = params.get("key") || "";
  var keySuffix = apiKey ? apiKey.slice(-8) : "anon";
  var TOKEN_KEY = "nikravan_token_" + keySuffix;
  var ANNOUNCE_DISMISS_KEY = "nikravan_announcement_dismissed_" + keySuffix;

  var token = null;
  var busy = false;
  var dead = false; // invalid key — keep the form closed

  var elMessages = document.getElementById("chat-messages");
  var elQuick = document.getElementById("quick-replies");
  var elScroll = document.getElementById("chat-scroll");
  var elForm = document.getElementById("input-bar");
  var elInput = document.getElementById("msg-input");
  var elSend = document.getElementById("btn-send");
  var elClose = document.getElementById("btn-close");
  var elBanner = null;

  function toEnglishDigits(s) {
    return String(s).replace(/[۰-۹]/g, function (d) {
      return "۰۱۲۳۴۵۶۷۸۹".indexOf(d);
    });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function renderBotContent(text, html) {
    // HTML is escaped server-side (md_to_html) — if it came, render it as-is
    if (html) {
      var div = document.createElement("div");
      div.innerHTML = html;
      // booking links: new tab; if the browser blocks it, at least the whole window (not inside the widget frame)
      var links = div.querySelectorAll("a");
      for (var i = 0; i < links.length; i++) {
        links[i].removeAttribute("target");
        links[i].setAttribute("onclick", "window.open(this.href,'_blank');return false;");
        links[i].setAttribute("rel", "noopener");
      }
      return div.innerHTML;
    }
    return escapeHtml(text);
  }

  function scrollToBottom() {
    elScroll.scrollTop = elScroll.scrollHeight;
  }

  // put the new question at the top of the viewport: the question and its options are seen together
  function scrollQuestionIntoView() {
    var msgs = elMessages.querySelectorAll(".msg.bot:not(.typing)");
    var last = msgs[msgs.length - 1];
    if (!last) { scrollToBottom(); return; }
    var boxRect = elScroll.getBoundingClientRect();
    var qRect = last.getBoundingClientRect();
    var delta = qRect.top - boxRect.top;
    elScroll.scrollTop += delta - 10;
  }

  function addMessage(role, text, html) {
    var div = document.createElement("div");
    div.className = "msg " + role;
    if (role === "bot") {
      div.innerHTML = renderBotContent(text, html);
    } else {
      div.textContent = text;
    }
    elMessages.appendChild(div);
    scrollToBottom();
    return div;
  }

  function showTyping() {
    hideTyping();
    var div = document.createElement("div");
    div.className = "msg bot typing";
    div.id = "typing";
    div.innerHTML = "<span></span><span></span><span></span>";
    elMessages.appendChild(div);
    scrollToBottom();
  }

  function hideTyping() {
    var t = document.getElementById("typing");
    if (t) t.remove();
  }

  function renderChips(replies, inputType) {
    elQuick.innerHTML = "";
    if (!replies || !replies.length) return;
    replies.forEach(function (label) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.textContent = label;
      b.addEventListener("click", function () { send(label); });
      elQuick.appendChild(b);
    });
    // options appear right under that same question — no manual scrolling by the user
    setTimeout(scrollQuestionIntoView, 60);
  }

  function setInputMode(inputType) {
    if (inputType === "chips" || dead) {
      elForm.classList.add("hidden");
    } else {
      elForm.classList.remove("hidden");
      elInput.focus();
    }
  }

  function showAnnouncement(text) {
    if (!text) return;
    try {
      if (localStorage.getItem(ANNOUNCE_DISMISS_KEY) === "1") return;
    } catch (e) {}
    if (elBanner) elBanner.remove();
    elBanner = document.createElement("div");
    elBanner.id = "announcement-banner";
    elBanner.innerHTML =
      '<div class="ann-text">' + escapeHtml(text) + "</div>" +
      '<button type="button" class="ann-close" title="بستن">✕</button>';
    elBanner.querySelector(".ann-close").addEventListener("click", function () {
      elBanner.remove();
      elBanner = null;
      try { localStorage.setItem(ANNOUNCE_DISMISS_KEY, "1"); } catch (e) {}
    });
    elScroll.insertBefore(elBanner, elMessages);
  }

  function applyReply(reply, done) {
    addMessage("bot", reply.text, reply.html);
    renderChips(reply.quick_replies, reply.input_type);
    setInputMode(reply.input_type);
    // put the new question at the top of the viewport: question + options/input are seen together
    setTimeout(scrollQuestionIntoView, 60);
  }

  function send(text) {
    if (busy || dead || !text || !text.trim()) return;
    busy = true;
    elSend.disabled = true;

    var clean = text.trim().slice(0, 2000);
    addMessage("user", clean);
    elInput.value = "";
    elQuick.innerHTML = "";
    showTyping();

    fetch("/api/chat/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token, text: clean }),
    })
      .then(function (r) {
        if (r.status === 404) {
          // session expired — start a new session and try again
          hideTyping();
          return startSession(true).then(function () {
            busy = false; elSend.disabled = false;
            addMessage("bot", "🔄 نشست گفتگو تازه‌سازی شد. لطفاً پیام خود را دوباره ارسال کنید.");
          });
        }
        if (r.status === 403) {
          hideTyping();
          busy = false; elSend.disabled = false;
          dead = true;
          addMessage("bot", "⚠️ سرویس این مرکز موقتاً غیرفعال است.");
          setInputMode("chips");
          return null;
        }
        if (r.status === 429) {
          hideTyping();
          busy = false; elSend.disabled = false;
          addMessage("bot", "⚠️ پیام‌ها بیش از حد مجاز است. لطفاً کمی صبر کنید.");
          return null;
        }
        return r.json();
      })
      .then(function (data) {
        if (!data) return;
        hideTyping();
        setTimeout(function () {
          applyReply(data.reply, data.done);
          busy = false;
          elSend.disabled = false;
        }, 450);
      })
      .catch(function () {
        hideTyping();
        busy = false; elSend.disabled = false;
        addMessage("bot", "⚠️ خطای اتصال. لطفاً دوباره تلاش کنید.");
      });
  }

  function startSession(isRenew) {
    if (dead) return;
    var stored = null;
    try { stored = localStorage.getItem(TOKEN_KEY); } catch (e) {}
    if (isRenew) stored = null;

    return fetch("/api/chat/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(stored ? { api_key: apiKey, token: stored } : { api_key: apiKey }),
    })
      .then(function (r) {
        if (r.status === 401 || r.status === 403) {
          dead = true;
          hideTyping();
          elMessages.innerHTML = "";
          addMessage("bot", "⚠️ کلید API نامعتبر است. لطفاً با مدیر سایت تماس بگیرید.");
          setInputMode("chips");
          return null;
        }
        return r.json();
      })
      .then(function (data) {
        if (!data) return;
        token = data.token;
        try { localStorage.setItem(TOKEN_KEY, token); } catch (e) {}
        elMessages.innerHTML = "";
        showAnnouncement(data.announcement);
        (data.messages || []).forEach(function (m) { addMessage("bot", m.text, m.html); });
        renderChips(data.quick_replies, data.input_type);
        setInputMode(data.input_type || "text");
        setTimeout(scrollQuestionIntoView, 100);
      })
      .catch(function () {
        if (dead) return;
        addMessage("bot", "⚠️ خطای اتصال. لطفاً صفحه را رفرش کنید.");
      });
  }

  elForm.addEventListener("submit", function (e) {
    e.preventDefault();
    send(elInput.value);
  });

  // ── mobile: keep the keyboard from covering the input/messages ──
  // On Android the iframe is resizable; when the keyboard opens, the whole parent page is zoomed/scrolled.
  // Safe approach: notify the parent so it keeps the frame in the visible area + size the app with the real vvh.
  var vv = window.visualViewport;
  if (vv) {
    var applyVv = function () {
      document.documentElement.style.setProperty("--vvh", vv.height + "px");
      var app = document.getElementById("chat-app");
      app.style.height = vv.height + "px";
      try { window.scrollTo(0, 0); } catch (e) {}
      try {
        if (window.parent && window.parent !== window) {
          window.parent.postMessage("nikravan-widget-ensure-visible", "*");
        }
      } catch (e) {}
      setTimeout(scrollToBottom, 120);
      setTimeout(scrollToBottom, 400);
    };
    vv.addEventListener("resize", applyVv);
    vv.addEventListener("scroll", applyVv);
    applyVv();
  }
  elInput.addEventListener("focus", function () {
    setTimeout(function () {
      try { window.scrollTo(0, 0); } catch (e) {}
      if (window.parent && window.parent !== window) {
        try { window.parent.postMessage("nikravan-widget-ensure-visible", "*"); } catch (e) {}
      }
      scrollToBottom();
    }, 150);
    setTimeout(scrollToBottom, 450);
  });
  elInput.addEventListener("input", function () {
    // while typing: the widget frame must not go under the keyboard
    setTimeout(function () {
      try { window.scrollTo(0, 0); } catch (e) {}
      if (window.parent && window.parent !== window) {
        try { window.parent.postMessage("nikravan-widget-ensure-visible", "*"); } catch (e) {}
      }
      scrollToBottom();
    }, 50);
  });

  elClose.addEventListener("click", function () {
    try { window.parent.postMessage("nikravan-widget-close", "*"); } catch (e) {}
  });

  startSession(false);
})();
