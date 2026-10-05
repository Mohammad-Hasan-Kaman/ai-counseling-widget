(function () {
  "use strict";
  // Demo chat widget loader — one-line embed:
  // <script src="https://WIDGET_HOST/static/widget.js" data-origin="https://WIDGET_HOST" async></script>

  var script = document.currentScript || (function () {
    var s = document.getElementsByTagName("script");
    return s[s.length - 1];
  })();
  var origin = (script && script.getAttribute("data-origin")) || script.src.replace(/\/static\/widget\.js.*$/, "");
  if (!origin || origin.indexOf("http") !== 0) origin = "";
  var apiKey = (script && script.getAttribute("data-key")) || "";

  var OPEN_KEY = "demo_widget_open";

  var css = [
    "#demo-widget-root{position:fixed;bottom:20px;left:20px;z-index:2147483000;font-family:Vazirmatn,Tahoma,sans-serif;}",
    "#demo-widget-bubble{width:88px;height:88px;border-radius:50%;background:#2E5E4E;border:none;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25);display:flex;align-items:center;justify-content:center;transition:transform .15s ease;padding:4px;overflow:hidden;}",
    "#demo-widget-bubble:hover{transform:scale(1.07);}",
    "#demo-widget-bubble .demo-ai-wrap{display:flex;flex-direction:column;align-items:center;justify-content:center;line-height:1.35;user-select:none;}",
    "#demo-widget-bubble .demo-ai-badge{font-family:Vazirmatn,Tahoma,sans-serif;font-size:14px;font-weight:800;color:#fff;letter-spacing:0;white-space:nowrap;text-align:center;padding:0 2px;line-height:1.4;}",
    "#demo-widget-frame{position:fixed;bottom:92px;left:20px;width:380px;height:580px;max-height:calc(100vh - 120px);border:none;border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,.22);z-index:2147483001;display:none;background:#fff;overflow:hidden;}",
    "#demo-widget-frame.open{display:block;}",
    "@media (max-width:480px){",
    /* dvh: real visible height — when the keyboard opens the widget frame shrinks and the input/options stay above the keyboard */
    "  #demo-widget-frame.open{position:fixed;inset:0;width:100vw;height:100dvh;max-height:none;border-radius:0;bottom:0;left:0;}",
    "  #demo-widget-root.open{display:none;}",
    "}"
  ].join("\n");

  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  var brand = script && script.getAttribute("data-brand");
  if (!brand) brand = "گفتگو با ما";

  var root = document.createElement("div");
  root.id = "demo-widget-root";

  var bubble = document.createElement("button");
  bubble.id = "demo-widget-bubble";
  bubble.setAttribute("aria-label", brand);
  bubble.title = brand;
  bubble.innerHTML = '<span class="demo-ai-wrap" aria-hidden="true"><span class="demo-ai-badge">دستیار</span><span class="demo-ai-badge">انتخاب</span><span class="demo-ai-badge">مشاور</span></span>';

  var frame = document.createElement("iframe");
  frame.id = "demo-widget-frame";
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
      // let the host page return to its natural position after the widget closes
      try { window.scrollTo(0, 0); } catch (e) {}
      try { window.parent.scrollTo(0, 0); } catch (e) {}
    }
    try { localStorage.setItem(OPEN_KEY, open ? "1" : "0"); } catch (e) {}
  }

  bubble.addEventListener("click", function () { toggle(true); });

  window.addEventListener("message", function (e) {
    if (e.origin !== origin) return;
    if (e.data === "demo-widget-close") toggle(false);
    if (e.data === "demo-widget-ensure-visible") {
      // the mobile keyboard has zoomed/scrolled the whole page — bring the frame back inside the visible area
      try {
        if (window.innerWidth <= 480) {
          // fullscreen mode: wait for the browser to finish laying out with the new dvh, then scroll the page to the top
          setTimeout(function () { window.scrollTo(0, 0); }, 120);
        }
        window.scrollTo(0, 0);
      } catch (err) {}
    }
  });

  try { if (localStorage.getItem(OPEN_KEY) === "1") toggle(true); } catch (e) {}
})();
