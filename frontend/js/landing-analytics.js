(function () {
  const VERSION = "2026.10.06";
  const KEY = "r360_landing";

  function read() {
    try { return JSON.parse(localStorage.getItem(KEY) || "{}"); } catch { return {}; }
  }
  function write(data) {
    localStorage.setItem(KEY, JSON.stringify(data));
  }
  function id(prefix) {
    return prefix + "_" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
  }
  function params() {
    return new URLSearchParams(location.search);
  }

  const store = read();
  if (!store.visitor_id) store.visitor_id = id("v");
  if (!store.session_id) store.session_id = id("s");
  const q = params();
  ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"].forEach((key) => {
    if (q.get(key)) store[key] = q.get(key);
  });
  store.landing_version = VERSION;
  store.variant = store.variant || "A";
  write(store);

  const queue = [];
  let thirdParty = store.analytics_consent === "granted";

  function device() {
    return window.matchMedia("(max-width: 900px)").matches ? "mobile" : "desktop";
  }

  function payload(name, properties) {
    return {
      event_name: name,
      event_data: properties || {},
      page: location.pathname,
      section: (properties && properties.section) || "",
      session_id: store.session_id,
      visitor_id: store.visitor_id,
      landing_version: VERSION,
      variant: store.variant,
      utm_source: store.utm_source || "",
      utm_medium: store.utm_medium || "",
      utm_campaign: store.utm_campaign || "",
      utm_content: store.utm_content || "",
      utm_term: store.utm_term || "",
      referrer: document.referrer || "",
      device_type: device(),
      path: location.pathname + location.search
    };
  }

  function beacon(body) {
    const json = JSON.stringify(body);
    if (navigator.sendBeacon) {
      const ok = navigator.sendBeacon("/api/public/events", new Blob([json], { type: "application/json" }));
      if (ok) return;
    }
    fetch("/api/public/events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: json,
      keepalive: true
    }).catch(() => {});
  }

  function forward(name, properties) {
    if (!thirdParty) return;
    const ga = window.gtag;
    const fb = window.fbq;
    if (typeof ga === "function") ga("event", name, properties || {});
    const map = {
      landing_view: "PageView",
      contact_form_submit: "Lead",
      signup_complete: "CompleteRegistration",
      whatsapp_click: "Contact",
      email_click: "Contact",
      phone_click: "Contact"
    };
    if (typeof fb === "function" && map[name]) fb("track", map[name], properties || {});
    if (typeof fb === "function" && !map[name]) fb("trackCustom", name, properties || {});
  }

  function trackEvent(name, properties) {
    const body = payload(name, properties);
    queue.push(body);
    beacon(body);
    forward(name, properties);
    window.dispatchEvent(new CustomEvent("r360:event", { detail: body }));
  }

  function setConsent(value) {
    store.analytics_consent = value;
    write(store);
    thirdParty = value === "granted";
    if (thirdParty) loadPixels();
  }

  function loadPixels() {
    const ga = document.body.dataset.ga;
    const pixel = document.body.dataset.pixel;
    if (ga && !window.gtag) {
      const s = document.createElement("script");
      s.async = true;
      s.src = "https://www.googletagmanager.com/gtag/js?id=" + encodeURIComponent(ga);
      document.head.appendChild(s);
      window.dataLayer = window.dataLayer || [];
      window.gtag = function () { window.dataLayer.push(arguments); };
      window.gtag("js", new Date());
      window.gtag("config", ga, { send_page_view: false });
    }
    if (pixel && !window.fbq) {
      const n = window.fbq = function () { n.callMethod ? n.callMethod.apply(n, arguments) : n.queue.push(arguments); };
      if (!window._fbq) window._fbq = n;
      n.push = n; n.loaded = true; n.version = "2.0"; n.queue = [];
      const s = document.createElement("script");
      s.async = true;
      s.src = "https://connect.facebook.net/en_US/fbevents.js";
      document.head.appendChild(s);
      window.fbq("init", pixel);
    }
  }

  const depths = new Set();
  function onScroll() {
    const doc = document.documentElement;
    const max = doc.scrollHeight - doc.clientHeight;
    if (max <= 0) return;
    const pct = Math.round((doc.scrollTop / max) * 100);
    [25, 50, 75, 90].forEach((mark) => {
      if (pct >= mark && !depths.has(mark)) {
        depths.add(mark);
        trackEvent("scroll_" + mark, { section: "page" });
      }
    });
  }

  window.R360Analytics = { trackEvent, setConsent, version: VERSION, store, loadPixels };
  document.addEventListener("click", (event) => {
    const node = event.target.closest("[data-track]");
    if (!node) return;
    let extra = {};
    try { extra = JSON.parse(node.dataset.trackProps || "{}"); } catch { extra = {}; }
    trackEvent(node.dataset.track, extra);
  });
  window.addEventListener("scroll", onScroll, { passive: true });
  if (thirdParty) loadPixels();
  trackEvent("landing_view", { section: document.body.dataset.page || "home" });
})();
