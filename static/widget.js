(function () {
  "use strict";
  // لودر ویجت چت نیک‌روان — یک خط embed:
  // <script src="https://WIDGET_HOST/static/widget.js" data-origin="https://WIDGET_HOST" async></script>

  var script = document.currentScript || (function () {
    var s = document.getElementsByTagName("script");
    return s[s.length - 1];
  })();
  var origin = (script && script.getAttribute("data-origin")) || script.src.replace(/\/static\/widget\.js.*$/, "");
  if (!origin || origin.indexOf("http") !== 0) origin = "";
  var apiKey = (script && script.getAttribute("data-key")) || "";

  var OPEN_KEY = "nikravan_widget_open";

  var css = [
    "#nikravan-widget-root{position:fixed;bottom:20px;left:20px;z-index:2147483000;font-family:Vazirmatn,Tahoma,sans-serif;}",
    "#nikravan-widget-bubble{width:88px;height:88px;border-radius:50%;background:#2E5E4E;border:none;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25);display:flex;align-items:center;justify-content:center;transition:transform .15s ease;padding:4px;overflow:hidden;}",
    "#nikravan-widget-bubble:hover{transform:scale(1.07);}",
    "#nikravan-widget-bubble .nikravan-ai-wrap{display:flex;flex-direction:column;align-items:center;justify-content:center;line-height:1.35;user-select:none;}",
    "#nikravan-widget-bubble .nikravan-ai-badge{font-family:Vazirmatn,Tahoma,sans-serif;font-size:14px;font-weight:800;color:#fff;letter-spacing:0;white-space:nowrap;text-align:center;padding:0 2px;line-height:1.4;}",
    "#nikravan-widget-frame{position:fixed;bottom:92px;left:20px;width:380px;height:580px;max-height:calc(100vh - 120px);border:none;border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,.22);z-index:2147483001;display:none;background:#fff;overflow:hidden;}",
    "#nikravan-widget-frame.open{display:block;}",
    "@media (max-width:480px){",
    /* dvh: ارتفاع دیده‌شده واقعی — وقتی کیبورد باز می‌شود قاب ویجت جمع می‌شود و ورودی/گزینه‌ها روی کیبورد می‌مانند */
    "  #nikravan-widget-frame.open{position:fixed;inset:0;width:100vw;height:100dvh;max-height:none;border-radius:0;bottom:0;left:0;}",
    "  #nikravan-widget-root.open{display:none;}",
    "}"
  ].join("\n");

  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  var brand = script && script.getAttribute("data-brand");
  if (!brand) brand = "گفتگو با ما";

  var root = document.createElement("div");
  root.id = "nikravan-widget-root";

  var bubble = document.createElement("button");
  bubble.id = "nikravan-widget-bubble";
  bubble.setAttribute("aria-label", brand);
  bubble.title = brand;
  bubble.innerHTML = '<span class="nikravan-ai-wrap" aria-hidden="true"><span class="nikravan-ai-badge">دستیار</span><span class="nikravan-ai-badge">انتخاب</span><span class="nikravan-ai-badge">مشاور</span></span>';

  var frame = document.createElement("iframe");
  frame.id = "nikravan-widget-frame";
  frame.src = origin + "/widget?key=" + encodeURIComponent(apiKey);
  frame.title = "چت سامانه پذیرش";
  frame.allow = "clipboard-write";

  root.appendChild(bubble);
  document.body.appendChild(root);
  document.body.appendChild(frame);

  function toggle(open) {
    if (open === undefined) open = !frame.classList.contains("open");
    frame.classList.toggle("open", open);
    root.classList.toggle("open", open);
    if (!open) {
      // اجازه بده صفحهٔ میزبان بعد از بستن ویجت به جای طبیعی‌اش برگردد
      try { window.scrollTo(0, 0); } catch (e) {}
      try { window.parent.scrollTo(0, 0); } catch (e) {}
    }
    try { localStorage.setItem(OPEN_KEY, open ? "1" : "0"); } catch (e) {}
  }

  bubble.addEventListener("click", function () { toggle(true); });

  window.addEventListener("message", function (e) {
    if (e.origin !== origin) return;
    if (e.data === "nikravan-widget-close") toggle(false);
    if (e.data === "nikravan-widget-ensure-visible") {
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

  try { if (localStorage.getItem(OPEN_KEY) === "1") toggle(true); } catch (e) {}
})();
