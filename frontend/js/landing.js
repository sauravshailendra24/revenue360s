(function () {
  const fileMode = location.protocol === "file:";
  const href = (path) => fileMode ? ({
    "/": "index.html", "/product": "product.html", "/pricing": "pricing.html", "/solutions": "solutions.html",
    "/demo": "demo.html", "/contact": "contact.html", "/privacy": "privacy.html", "/terms": "terms.html", "/app": "app.html"
  }[path] || path) : path;
  const asset = (path) => fileMode ? path.replace("/static/", "") : path;
  const cfg = window.R360_PUBLIC || {};
  const page = document.body.dataset.page || "home";
  if (cfg.ga) document.body.dataset.ga = cfg.ga;
  if (cfg.pixel) document.body.dataset.pixel = cfg.pixel;
  const mark = '<svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" fill="#14171c"/><path d="M8 20.5c3.2-6.4 6.2-9.5 8-9.5s4.8 3.1 8 9.5" stroke="#c4a574" stroke-width="1.8" stroke-linecap="round"/><circle cx="16" cy="11" r="2" fill="#efe8dc"/></svg>';
  const header = document.querySelector("[data-nav]");
  if (header) {
    header.className = "site-nav";
    header.innerHTML = `<div class="wrap"><a class="logo" href="${href("/")}">${mark} Revenue<span>360s</span></a>
      <nav class="nav-links" aria-label="Primary">
        <a href="${href("/product")}" ${page === "product" ? 'aria-current="page"' : ""}>Product</a>
        <a href="${href("/")}#journey">How it works</a>
        <a href="${href("/solutions")}" ${page === "solutions" ? 'aria-current="page"' : ""}>Solutions</a>
        <a href="${href("/pricing")}" ${page === "pricing" ? 'aria-current="page"' : ""}>Pricing</a>
        <a href="${href("/demo")}" ${page === "demo" ? 'aria-current="page"' : ""}>Demo</a>
      </nav>
      <div class="nav-cta">
        <a class="btn ghost" href="${href("/app")}" data-track="login_click" data-track-props='{"location":"nav"}'>Log in</a>
        <a class="btn" href="${href("/app")}" data-track="signup_click" data-track-props='{"location":"nav","cta":"start"}'>Start free</a>
      </div>
      <button class="btn ghost menu-btn" type="button" aria-expanded="false" aria-controls="menu">Menu</button></div>`;
  }
  const menu = document.createElement("div");
  menu.className = "menu";
  menu.id = "menu";
  menu.innerHTML = `<div class="menu-panel"><button class="btn ghost" type="button" id="menu-close">Close</button>
    <a href="${href("/product")}">Product</a><a href="${href("/")}#journey">How it works</a>
    <a href="${href("/demo")}">Demo</a><a href="${href("/pricing")}">Pricing</a><a href="${href("/contact")}">Contact</a>
    <a href="${href("/app")}" data-track="login_click" data-track-props='{"location":"menu"}'>Log in</a>
    <a class="btn" href="${href("/app")}" data-track="signup_click" data-track-props='{"location":"menu"}'>Start free</a></div>`;
  document.body.appendChild(menu);
  const toggle = header && header.querySelector(".menu-btn");
  const setOpen = (open) => { menu.classList.toggle("open", open); document.body.classList.toggle("nav-open", open); if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false"); };
  if (toggle) toggle.onclick = () => setOpen(true);
  menu.querySelector("#menu-close").onclick = () => setOpen(false);
  menu.onclick = (event) => { if (event.target === menu) setOpen(false); };
  const nav = document.querySelector(".site-nav");
  const onScroll = () => nav && nav.classList.toggle("stuck", window.scrollY > 8);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
  const footer = document.querySelector("[data-footer]");
  if (footer) {
    footer.className = "site-footer";
    footer.innerHTML = `<div class="wrap foot"><div><strong>Revenue360s</strong><div>Growth workspace for prospecting, outreach, and follow-up.</div></div>
      <nav aria-label="Footer"><a href="${href("/product")}">Product</a><a href="${href("/pricing")}">Pricing</a><a href="${href("/solutions")}">Solutions</a><a href="${href("/demo")}">Demo</a><a href="${href("/contact")}">Contact</a><a href="${href("/privacy")}">Privacy</a><a href="${href("/terms")}">Terms</a></nav></div>
      <div class="wrap legal">${cfg.verified ? "" : "Sales email and phone are not confirmed. They live in landing-config.js so they can be corrected in one place."}</div>`;
  }
  document.querySelectorAll("[data-contact]").forEach((node) => {
    const kind = node.dataset.contact;
    if (kind === "email") { node.href = "mailto:" + cfg.salesEmail; node.textContent = cfg.salesEmail; }
    if (kind === "phone") { node.href = "tel:" + cfg.salesPhoneTel; node.textContent = cfg.salesPhoneDisplay; }
    if (kind === "whatsapp") node.href = cfg.whatsappUrl;
  });
  document.querySelectorAll("[data-product]").forEach((block) => {
    const buttons = [...block.querySelectorAll(".tabs button")];
    const panels = [...block.querySelectorAll("[data-panel]")];
    buttons.forEach((button) => button.onclick = () => {
      buttons.forEach((item) => item.classList.toggle("active", item === button));
      panels.forEach((panel) => { panel.hidden = panel.dataset.panel !== button.dataset.tab; });
      if (window.R360Analytics) window.R360Analytics.trackEvent("product_tab_view", { section: "product", tab: button.dataset.tab });
    });
  });
  document.querySelectorAll("[data-shot]").forEach((shot) => {
    const img = shot.querySelector("img");
    const replica = shot.querySelector("[data-replica]");
    if (!img || !replica) return;
    img.src = asset(img.getAttribute("data-src") || img.src);
    img.addEventListener("load", () => { replica.hidden = true; });
    img.addEventListener("error", () => { img.hidden = true; replica.hidden = false; });
  });
  document.querySelectorAll("[data-insight]").forEach((block) => {
    const picks = [...block.querySelectorAll(".lead-pick")];
    const box = block.querySelector("[data-insight-body]");
    const stories = {
      acme: ["Why this lead?", "Hiring sales roles, expanding, active site.", "Personalized outreach", "WhatsApp + Email", "3-step sequence, stops on reply"],
      clinic: ["Why this lead?", "Service business, WhatsApp already in use.", "Book a walkthrough", "WhatsApp", "Follow up in 2 days"]
    };
    const paint = (id) => {
      const [why, evidence, action, channel, auto] = stories[id];
      box.innerHTML = `<article><h3>${why}</h3><p>${evidence}</p></article><article><h3>Recommended</h3><p>${action}</p></article><article><h3>Channel</h3><p>${channel}</p></article><article><h3>Automation</h3><p>${auto}</p></article>`;
      if (window.R360Analytics) window.R360Analytics.trackEvent("insight_interaction", { section: "insights", card: id });
    };
    picks.forEach((pick) => pick.onclick = () => paint(pick.dataset.lead));
    if (picks[0]) paint(picks[0].dataset.lead);
  });
  const engine = document.querySelector(".engine");
  if (engine && window.matchMedia("(hover: hover)").matches) {
    const stage = engine.querySelector(".engine-stage");
    engine.addEventListener("mousemove", (event) => {
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      const rect = engine.getBoundingClientRect();
      stage.style.transform = `rotateY(${((event.clientX - rect.left) / rect.width - .5) * 6}deg) rotateX(${-((event.clientY - rect.top) / rect.height - .5) * 5}deg)`;
    });
    engine.addEventListener("mouseleave", () => { stage.style.transform = ""; });
  }
  if (engine && "IntersectionObserver" in window) {
    new IntersectionObserver((entries) => entries.forEach((entry) => engine.classList.toggle("live", entry.isIntersecting)), { threshold: .3 }).observe(engine);
    if (window.R360Analytics) window.R360Analytics.trackEvent("hero_view", { section: "hero" });
  }
  const seen = new Set();
  if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver((entries) => entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      const name = entry.target.dataset.view;
      if (!name || seen.has(name)) return;
      seen.add(name);
      if (window.R360Analytics) window.R360Analytics.trackEvent(name, { section: entry.target.dataset.section || "" });
    }), { threshold: .35 });
    document.querySelectorAll("[data-view]").forEach((node) => observer.observe(node));
  }
  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ch === "&" ? "\u0026amp;" : ch === "<" ? "\u0026lt;" : ch === ">" ? "\u0026gt;" : ch === "\"" ? "\u0026quot;" : "\u0026#39;");
  }
  async function loadPlans(target) {
    if (!target) return;
    try {
      const response = await fetch("/api/public/plans");
      if (!response.ok) throw new Error("plans");
      const data = await response.json();
      if (!(data.plans || []).length) return;
      target.innerHTML = data.plans.slice(0, 3).map((plan, index) => `<article class="plan ${index === 1 ? "featured" : ""}"><h3>${escapeHtml(plan.name || plan.tier)}</h3><div class="price">₹${Number(plan.price || 0).toLocaleString("en-IN")}<small> / month</small></div><p>${escapeHtml(plan.tier || "")} · ${escapeHtml(plan.mode || "standard")}</p><ul><li>Catalog price</li><li>Access after payment is verified</li><li>${escapeHtml(plan.service_code || "Workspace")}</li></ul><a class="btn ${index === 1 ? "" : "ghost"}" href="${href("/app")}" data-track="pricing_cta_click" data-track-props='${JSON.stringify({ plan: plan.tier })}'>Start</a></article>`).join("");
    } catch { /* keep unlabeled tiers */ }
  }
  loadPlans(document.querySelector("[data-plans]"));
  const form = document.querySelector("#contact-form");
  if (form) {
    let started = false;
    form.addEventListener("focusin", () => { if (!started) { started = true; window.R360Analytics && window.R360Analytics.trackEvent("contact_form_start", { section: "contact" }); } });
    form.onsubmit = async (event) => {
      event.preventDefault();
      const status = document.querySelector("#form-status");
      const body = Object.fromEntries(new FormData(form).entries());
      body.landing_version = (window.R360Analytics && window.R360Analytics.version) || "";
      body.source = page;
      status.textContent = "Sending…";
      try {
        const response = await fetch("/api/public/contact", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "Could not send");
        form.reset();
        status.textContent = "Received. Reply goes to the email you entered.";
        window.R360Analytics && window.R360Analytics.trackEvent("contact_form_submit", { section: "contact" });
      } catch (error) { status.textContent = error.message; }
    };
  }
  if (!localStorage.getItem("r360_cookie")) {
    const bar = document.createElement("div");
    bar.className = "cookie";
    bar.innerHTML = `<p>First-party page events are recorded. Google and Meta load only if you allow them.</p><div class="row"><button class="btn" type="button" id="allow">Allow analytics</button><button class="btn ghost" type="button" id="deny">First-party only</button></div>`;
    document.body.appendChild(bar);
    bar.querySelector("#allow").onclick = () => { localStorage.setItem("r360_cookie", "granted"); window.R360Analytics && window.R360Analytics.setConsent("granted"); bar.remove(); };
    bar.querySelector("#deny").onclick = () => { localStorage.setItem("r360_cookie", "denied"); window.R360Analytics && window.R360Analytics.setConsent("denied"); bar.remove(); };
  }
  if (fileMode) {
    const map = { "/": "index.html", "/product": "product.html", "/pricing": "pricing.html", "/solutions": "solutions.html", "/demo": "demo.html", "/contact": "contact.html", "/privacy": "privacy.html", "/terms": "terms.html", "/app": "app.html" };
    document.querySelectorAll("a[href^='/']").forEach((anchor) => {
      const raw = anchor.getAttribute("href");
      const [path, hash] = raw.split("#");
      if (map[path]) anchor.setAttribute("href", map[path] + (hash ? "#" + hash : ""));
    });
  }
})();
