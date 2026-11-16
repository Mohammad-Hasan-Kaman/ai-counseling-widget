(function () {
  "use strict";

  // کلید API از query iframe
  var params = new URLSearchParams(window.location.search);
  var apiKey = params.get("key") || "";
  var keySuffix = apiKey ? apiKey.slice(-8) : "anon";
  var TOKEN_KEY = "counseling_token_" + keySuffix;
  var ANNOUNCE_DISMISS_KEY = "counseling_announcement_dismissed_" + keySuffix;

  var token = null;
  var busy = false;
  var dead = false; // کلید نامعتبر — فرم بسته بماند

  var elMessages = document.getElementById("chat-messages");
  var elQuick = document.getElementById("quick-replies");
  var elScroll = document.getElementById("chat-scroll");
  var elForm = document.getElementById("input-bar");
  var elInput = document.getElementById("msg-input");
  var elSend = document.getElementById("btn-send");
  var elClose = document.getElementById("btn-close");
  var elBanner = null;

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function renderBotContent(text, html) {
    // html سمت سرور escape شده (md_to_html) — اگر آمد همان را رندر کن
    if (html) {
      var div = document.createElement("div");
      div.innerHTML = html;
      // لینک‌های رزرو: تب جدید؛ اگر مرورگر بلاک کرد، حداقل کل پنجره (نه داخل فریم ویجت)
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

  // سؤال تازه را بالای ناحیه دید بگذار: سؤال + گزینه‌هایش با هم دیده شوند
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
    // گزینه‌ها زیر همان سؤال دیده شوند — بدون اسکرول دستی کاربر
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

  // ── مقاومت در برابر قطعی: ۵ تلاشِ خودکار قبل از گفتن «ارتباط برقرار نشد» ──
  var NET_ATTEMPTS = 5;

  function delay(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }

  function fetchWithRetry(url, options) {
    var left = NET_ATTEMPTS;
    function once() {
      return fetch(url, options).then(
        function (r) {
          if (r.status >= 500 && left > 1) {
            left--;
            return delay(400).then(once);
          }
          return r;
        },
        function (err) {
          if (left <= 1) throw err;
          left--;
          return delay(400).then(once);
        }
      );
    }
    return once();
  }

  // دکمهٔ «تلاش مجدد» زیر پیام خطا — رفتارش را فراخواننده تعیین می‌کند
  // (آخرین پیام را دوباره بفرستد یا سشن را دوباره باز کند)
  function renderLocalRetry(onRetry) {
    elQuick.innerHTML = "";
    var b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    b.textContent = "تلاش مجدد";
    b.addEventListener("click", onRetry);
    elQuick.appendChild(b);
    setTimeout(scrollQuestionIntoView, 60);
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
    // سؤال تازه را بالای دید بگذار: سؤال + گزینه‌ها/ورودی با هم دیده شوند
    setTimeout(scrollQuestionIntoView, 60);
  }

  var lastSend = null;

  function send(text, opts) {
    opts = opts || {};
    if (busy || dead || !text || !text.trim()) return;
    busy = true;
    elSend.disabled = true;

    var clean = text.trim().slice(0, 2000);
    lastSend = clean;
    // تلاش مجدد بعد از خطا: حباب کاربر از قبل هست، دوباره اضافه نشود
    if (!opts.skipEcho) addMessage("user", clean);
    elInput.value = "";
    elQuick.innerHTML = "";
    showTyping();

    fetchWithRetry("/api/chat/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token, text: clean }),
    })
      .then(function (r) {
        if (r.status === 404) {
          // سشن منقضی شده — سشن جدید بگیر و دوباره تلاش کن
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
        // سقف وسط چت تمام شده — قفل (Q19)
        if (data.locked) {
          dead = true;
          busy = false;
          elSend.disabled = false;
          addMessage("bot", data.lock_message || "ظرفیت پذیرش آنلاین این مرکز تکمیل شده است.");
          setInputMode("chips");
          return;
        }
        setTimeout(function () {
          applyReply(data.reply, data.done);
          busy = false;
          elSend.disabled = false;
        }, 450);
      })
      .catch(function () {
        // ۵ تلاشِ خودکار هم جواب نداد — بگوییم بعداً تلاش کنند + دکمه
        hideTyping();
        busy = false; elSend.disabled = false;
        addMessage("bot", "⚠️ ارتباط برقرار نشد. لطفاً کمی بعد دوباره تلاش کنید.");
        renderLocalRetry(function () { send(lastSend, { skipEcho: true }); });
      });
  }

  var lastStartRenew = false;

  function startSession(isRenew) {
    if (dead) return Promise.resolve(null);
    lastStartRenew = !!isRenew;
    var stored = null;
    try { stored = localStorage.getItem(TOKEN_KEY); } catch (e) {}
    if (isRenew) stored = null;

    return fetchWithRetry("/api/chat/session", {
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
        // قفل پلن/سقف — از لحظه باز شدن (Q19/Q25)
        if (data.locked) {
          dead = true;
          elMessages.innerHTML = "";
          addMessage("bot", data.lock_message || "ظرفیت پذیرش آنلاین این مرکز تکمیل شده است.");
          setInputMode("chips");
          return;
        }
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
        addMessage("bot", "⚠️ ارتباط برقرار نشد. لطفاً کمی بعد دوباره تلاش کنید.");
        renderLocalRetry(function () { startSession(lastStartRenew); });
      });
  }

  elForm.addEventListener("submit", function (e) {
    e.preventDefault();
    send(elInput.value);
  });

  // ── موبایل: جلوگیری از پوشیده شدن ورودی/پیام‌ها توسط کیبورد ──
  // روی اندروید iframe مقیاس‌پذیر است؛ وقتی کیبورد باز می‌شود کل صفحه والد zoom/scroll می‌شود.
  // راه مطمئن: اطلاع‌دادن به والد تا فریم را در ناحیه دیده‌شده نگه دارد + ارتفاع اپ با vvh واقعی.
  var vv = window.visualViewport;
  if (vv) {
    var applyVv = function () {
      document.documentElement.style.setProperty("--vvh", vv.height + "px");
      var app = document.getElementById("chat-app");
      app.style.height = vv.height + "px";
      try { window.scrollTo(0, 0); } catch (e) {}
      try {
        if (window.parent && window.parent !== window) {
          window.parent.postMessage("counseling-widget-ensure-visible", "*");
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
        try { window.parent.postMessage("counseling-widget-ensure-visible", "*"); } catch (e) {}
      }
      scrollToBottom();
    }, 150);
    setTimeout(scrollToBottom, 450);
  });
  elInput.addEventListener("input", function () {
    // حین تایپ: فریم ویجت نباید زیر کیبورد برود
    setTimeout(function () {
      try { window.scrollTo(0, 0); } catch (e) {}
      if (window.parent && window.parent !== window) {
        try { window.parent.postMessage("counseling-widget-ensure-visible", "*"); } catch (e) {}
      }
      scrollToBottom();
    }, 50);
  });

  elClose.addEventListener("click", function () {
    try { window.parent.postMessage("counseling-widget-close", "*"); } catch (e) {}
  });

  startSession(false);
})();
