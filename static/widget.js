(function () {
  "use strict";
  // Counseling chat widget loader — one-line embed:
  // <script src="https://WIDGET_HOST/static/widget.js" data-origin="https://WIDGET_HOST" async></script>

  var script = document.currentScript || (function () {
    var s = document.getElementsByTagName("script");
    return s[s.length - 1];
  })();
  var origin = (script && script.getAttribute("data-origin")) || script.src.replace(/\/static\/widget\.js.*$/, "");
  if (!origin || origin.indexOf("http") !== 0) origin = "";
  // نرمال‌سازی: e.origin مرورگر هرگز اسلش انتهایی ندارد — بدون این، مقایسه message هرگز نمی‌خورد و بستن ویجت کار نمی‌کرد
  origin = origin.replace(/\/+$/, "");
  var apiKey = (script && script.getAttribute("data-key")) || "";

  var OPEN_KEY = "counseling_widget_open";

  var css = [
    "#counseling-widget-root{position:fixed;bottom:20px;left:20px;z-index:2147483000;font-family:Vazirmatn,Tahoma,sans-serif;}",
    "#counseling-widget-bubble{width:88px;height:88px;border-radius:50%;background:#2E5E4E;border:none;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25);display:flex;align-items:center;justify-content:center;transition:transform .15s ease;padding:4px;overflow:hidden;}",
    "#counseling-widget-bubble:hover{transform:scale(1.07);}",
    "#counseling-widget-bubble .cw-ai-wrap{display:flex;flex-direction:column;align-items:center;justify-content:center;line-height:1.35;user-select:none;}",
    "#counseling-widget-bubble .cw-ai-badge{font-family:Vazirmatn,Tahoma,sans-serif;font-size:14px;font-weight:800;color:#fff;letter-spacing:0;white-space:nowrap;text-align:center;padding:0 2px;line-height:1.4;}",
    "#counseling-widget-frame{position:fixed;bottom:92px;left:20px;width:380px;height:580px;max-height:calc(100vh - 120px);border:none;border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,.22);z-index:2147483001;display:none;background:#fff;overflow:hidden;}",
    "#counseling-widget-frame.open{display:block;}",
    "@media (max-width:480px){",
    /* dvh: ارتفاع دیده‌شده واقعی — وقتی کیبورد باز می‌شود قاب ویجت جمع می‌شود و ورودی/گزینه‌ها روی کیبورد می‌مانند */
    "  #counseling-widget-frame.open{position:fixed;inset:0;width:100vw;height:100dvh;max-height:none;border-radius:0;bottom:0;left:0;}",
    "  #counseling-widget-root.open{display:none;}",
    "}"
  ].join("\n");

  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  var brand = script && script.getAttribute("data-brand");
  if (!brand) brand = "گفتگو با ما";

  var root = document.createElement("div");
  root.id = "counseling-widget-root";

  var bubble = document.createElement("button");
  bubble.id = "counseling-widget-bubble";
  bubble.setAttribute("aria-label", brand);
  bubble.title = brand;
  bubble.innerHTML = '<span class="cw-ai-wrap" aria-hidden="true"><span class="cw-ai-badge">دستیار</span><span class="cw-ai-badge">انتخاب</span><span class="cw-ai-badge">مشاور</span></span>';

  var frame = document.createElement("iframe");
  frame.id = "counseling-widget-frame";
  frame.src = origin + "/widget?key=" + encodeURIComponent(apiKey);
  frame.title = "چت سامانه پذیرش";
  frame.allow = "clipboard-write";

  function toggle(open) {
    if (open === undefined) open = !frame.classList.contains("open");
    frame.classList.toggle("open", open);
    root.classList.toggle("open", open);
    if (!open) {
      // فقط در حالت تمام‌صفحه (موبایل): کیبورد/زوم اندروید صفحهٔ میزبان را
      // جابه‌جا کرده و باید به جای طبیعی‌اش برگردد. روی دسکتاپ بستنِ ویجت
      // (کلیک بیرون یا دایره) نباید کاربر را از وسطِ صفحه به بالا پرت کند.
      if (window.innerWidth <= 480) {
        try { window.scrollTo(0, 0); } catch (e) {}
        try { window.parent.scrollTo(0, 0); } catch (e) {}
      }
    }
    try { localStorage.setItem(OPEN_KEY, open ? "1" : "0"); } catch (e) {}
  }

  // دایره هم باز می‌کند هم می‌بندد — قبلاً فقط باز می‌کرد و راهی برای بستن از سایت میزبان نبود
  bubble.addEventListener("click", function () { toggle(); });

  // کلیک بیرون از ویجت (روی سایت مقصد) آن را می‌بندد.
  // کلیک داخل iframe هرگز به این سند نمی‌رسد، پس فقط کلیکِ روی خودِ سایت می‌ماند
  // که باید به‌جز دایره، ویجت را ببندد.
  document.addEventListener("click", function (e) {
    if (!frame.classList.contains("open")) return;
    if (root.contains(e.target)) return;
    toggle(false);
  });

  window.addEventListener("message", function (e) {
    if (e.origin !== origin) return;
    if (e.data === "counseling-widget-close") toggle(false);
    if (e.data === "counseling-widget-ensure-visible") {
      // کیبورد موبایل کل صفحه را zoom/scroll کرده — قاب را برگردان داخل ناحیه دیده‌شده
      try {
        if (window.innerWidth <= 480) {
          // حالت تمام‌صفحه: صبر کن مرورگر layout را با dvh جدید کامل کند بعد صفحه را صفر کن
          setTimeout(function () { window.scrollTo(0, 0); }, 120);
        }
        window.scrollTo(0, 0);
      } catch (err) {}
    }
  });

  // mount: اگر اسکریپت قبل از body اجرا شود (async در head) صبر می‌کنیم تا رندر شکست نخورد
  function mount() {
    root.appendChild(bubble);
    document.body.appendChild(root);
    document.body.appendChild(frame);
    // «باز» فقط بعد از رفرش برمی‌گردد. رفتن به صفحهٔ دیگرِ سایت باید ویجت را
    // ببندد — قبلاً پرچم در localStorage می‌ماند و هر صفحهٔ تازه ویجتِ باز را
    // دوباره بالا می‌آورد. اگر همین‌جا پاک نشود، رفرشِ بعد از ناوبری هم باز می‌کند.
    var restore = false;
    try {
      var nav = performance.getEntriesByType ? performance.getEntriesByType("navigation") : [];
      var navType = (nav && nav[0] && nav[0].type) || "";
      restore = navType === "reload" && localStorage.getItem(OPEN_KEY) === "1";
      localStorage.setItem(OPEN_KEY, restore ? "1" : "0");
    } catch (e) {}
    if (restore) toggle(true);
  }
  if (document.body) {
    mount();
  } else {
    document.addEventListener("DOMContentLoaded", mount);
  }
})();
