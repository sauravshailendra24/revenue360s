const state = { token: localStorage.getItem("r360_token") || "", page: "today", runId: "", chatOpen: false };

const $ = (id) => document.getElementById(id);
const pageEl = () => $("page");

const NAV = [
  ["Overview", [["today", "Today"]]],
  ["Customers", [["inbox", "Inbox"], ["leads", "Leads"], ["accounts", "Accounts"]]],
  ["Growth", [["campaigns", "Campaigns"], ["sequences", "Sequences"], ["social_media", "Social"], ["ads", "Ads"], ["content", "Content"]]],
  ["Intelligence", [["search", "Search"], ["cloud", "Cloud"]]],
  ["Automation", [["work", "Work"], ["workflows", "Workflows"], ["templates", "Templates"]]],
  ["Settings", [["company", "Company"], ["settings", "Preferences"]]],
];

const TITLES = {
  today: "Today", inbox: "Inbox", leads: "Leads", accounts: "Accounts", campaigns: "Campaigns",
  sequences: "Sequences", social_media: "Social", ads: "Ads", content: "Content", search: "Search",
  cloud: "Cloud", work: "Work", workflows: "Workflows", templates: "Templates", company: "Company",
  settings: "Preferences", analytics: "Analytics", overview: "Analytics", home: "Today",
};

function toast(msg) {
  const el = $("toast");
  if (!el) return;
  el.textContent = typeof msg === "string" ? msg : JSON.stringify(msg);
  el.style.display = "block";
  setTimeout(() => { el.style.display = "none"; }, 2800);
}

function escapeHtml(value) {
  const map = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  };
  return String(value ?? "").replace(/[&<>"']/g, (ch) => map[ch]);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (!(options.body instanceof FormData) && options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const res = await fetch(path, { ...options, headers });
  if (res.status === 401) {
    localStorage.removeItem("r360_token");
    state.token = "";
    showAuth();
    throw new Error("Sign in first.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.message || "Request failed");
  return data;
}

async function quiet(path) {
  try { return await api(path); } catch { return null; }
}

function showAuth() {
  $("auth").classList.remove("hide");
  $("app").classList.add("hide");
  closeNav();
}

function showApp() {
  $("auth").classList.add("hide");
  $("app").classList.remove("hide");
  paintNav();
}

function paintNav() {
  const nav = document.querySelector(".nav");
  if (!nav) return;
  nav.innerHTML = NAV.map(([label, items]) => `<span class="nav-label">${label}</span>${items.map(([page, name]) => `<button data-page="${page}" class="${state.page === page ? "active" : ""}" ${state.page === page ? "aria-current=\"page\"" : ""}>${name}</button>`).join("")}`).join("");
  nav.querySelectorAll("button").forEach((btn) => {
    btn.onclick = () => go(btn.dataset.page);
  });
}

function go(page) {
  state.page = page;
  if (page !== "search") siState.projectId = "";
  if (page !== "cloud") { cloudState.step = 0; cloudState.planId = ""; }
  paintNav();
  closeNav();
  render();
}

function setNavOpen(open) {
  $("side").classList.toggle("open", open);
  $("nav-scrim").hidden = !open;
  $("nav-toggle").setAttribute("aria-expanded", open ? "true" : "false");
}

function closeNav() { setNavOpen(false); }

function render() {
  if (typeof stopSearchPoll === "function") stopSearchPoll();
  if (typeof closeDrawer === "function") closeDrawer();
  const title = $("topbar-title");
  if (title) title.textContent = TITLES[state.page] || "Revenue360s";
  const root = pageEl();
  root.innerHTML = "<p class='sub'>Loading…</p>";
  const pages = {
    today: renderToday,
    home: renderToday,
    work: renderWork,
    content: renderContent,
    audience: renderLeads,
    analytics: renderOverview,
    accounts: renderConnections,
    overview: renderOverview,
    inbox: renderInbox,
    leads: renderLeads,
    sequences: renderSequences,
    templates: renderTemplates,
    social_media: renderSocial,
    campaigns: renderCampaigns,
    workflows: renderWorkflows,
    email: renderEmail,
    sms: renderSms,
    whatsapp: renderWhatsapp,
    ads: renderAds,
    company: renderCompany,
    settings: renderConnections,
    search: renderSearch,
    cloud: renderCloud,
  };
  const fn = pages[state.page] || renderToday;
  fn(root).catch((err) => { root.innerHTML = `<p class="sub">${escapeHtml(err.message)}</p>`; });
  ensureChatDock();
}

$("tab-login") && ($("tab-login").onclick = () => { $("login-form").classList.remove("hide"); $("signup-form").classList.add("hide"); $("reset-form").classList.add("hide"); });
$("tab-signup") && ($("tab-signup").onclick = () => { $("signup-form").classList.remove("hide"); $("login-form").classList.add("hide"); $("reset-form").classList.add("hide"); });
$("tab-reset") && ($("tab-reset").onclick = () => { $("reset-form").classList.remove("hide"); $("login-form").classList.add("hide"); $("signup-form").classList.add("hide"); });

$("login-form") && ($("login-form").onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    const data = await api("/api/auth/login", { method: "POST", body: { email: fd.get("email"), password: fd.get("password") } });
    state.token = data.token;
    localStorage.setItem("r360_token", data.token);
    $("who").textContent = data.email;
    showApp();
    render();
  } catch (err) { $("auth-msg").textContent = err.message; }
});

$("signup-form") && ($("signup-form").onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    const data = await api("/api/auth/signup", { method: "POST", body: { full_name: fd.get("full_name"), email: fd.get("email"), password: fd.get("password") } });
    $("auth-msg").textContent = data.verification_code ? `Account created. Code ${data.verification_code}. Log in now.` : "Account created. Log in now.";
    $("tab-login").click();
  } catch (err) { $("auth-msg").textContent = err.message; }
});

$("send-reset") && ($("send-reset").onclick = async () => {
  const email = document.querySelector("#reset-form [name=email]").value;
  try {
    const data = await api("/api/auth/forgot", { method: "POST", body: { email } });
    $("auth-msg").textContent = data.reset_code ? `Reset code: ${data.reset_code}` : data.message;
  } catch (err) { $("auth-msg").textContent = err.message; }
});

$("reset-form") && ($("reset-form").onsubmit = async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  try {
    await api("/api/auth/reset", { method: "POST", body: { code: fd.get("code"), password: fd.get("password") } });
    $("auth-msg").textContent = "Password updated. Log in now.";
    $("tab-login").click();
  } catch (err) { $("auth-msg").textContent = err.message; }
});

$("logout") && ($("logout").onclick = () => { localStorage.removeItem("r360_token"); state.token = ""; showAuth(); });
$("nav-toggle") && ($("nav-toggle").onclick = () => setNavOpen(!$("side").classList.contains("open")));
$("nav-close") && ($("nav-close").onclick = closeNav);
$("nav-scrim") && ($("nav-scrim").onclick = closeNav);

if (state.token) {
  showApp();
  api("/api/me").then((me) => { $("who").textContent = me.email; render(); }).catch(showAuth);
} else {
  showAuth();
}

function ensureChatDock() {
  if ($("agent-dock")) return;
  const dock = document.createElement("div");
  dock.id = "agent-dock";
  dock.innerHTML = `<button class="btn" id="ask-r360" type="button">Ask Revenue360</button>
    <div id="agent-panel" class="card hide">
      <h3>Revenue360</h3>
      <p class="sub">What do you want to accomplish?</p>
      <div id="agent-thread"></div>
      <div id="agent-choices"></div>
      <div class="row"><input id="agent-input" placeholder="Type here…" /><button class="btn" id="agent-send" type="button">Send</button></div>
    </div>`;
  document.body.appendChild(dock);
  $("ask-r360").onclick = () => $("agent-panel").classList.toggle("hide");
  $("agent-send").onclick = sendAgentMessage;
  $("agent-input").addEventListener("keydown", (e) => { if (e.key === "Enter") sendAgentMessage(); });
}

async function sendAgentMessage() {
  const input = $("agent-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  const data = await api("/api/agent/chat", { method: "POST", body: { message, run_id: state.runId } });
  state.runId = data.id;
  drawAgent(data);
}

function drawAgent(data) {
  const thread = $("agent-thread");
  const choices = $("agent-choices");
  if (!thread) return;
  thread.innerHTML = (data.conversation || []).slice(-8).map((m) => `<p class="sub"><b>${escapeHtml(m.role)}</b> ${escapeHtml(m.text)}</p>`).join("");
  const ask = data.ask || data.last_question || {};
  if (ask.options) {
    choices.innerHTML = ask.options.map((opt) => `<button class="btn ghost" data-choice="${escapeHtml(opt.id)}" type="button">${escapeHtml(opt.label)}</button>`).join("");
    choices.querySelectorAll("button").forEach((btn) => {
      btn.onclick = async () => {
        const res = await api("/api/agent/chat", { method: "POST", body: { run_id: data.id, field: ask.field, choice: btn.dataset.choice } });
        state.runId = res.id;
        drawAgent(res);
        if (state.page === "today" || state.page === "work") render();
      };
    });
  } else {
    choices.innerHTML = data.workflow && data.phase === "confirmation"
      ? `<p class="sub">Ready. Review on Work, then start.</p><button class="btn" id="start-from-chat" type="button">Start</button>`
      : "";
    const start = $("start-from-chat");
    if (start) start.onclick = async () => { await api(`/api/agent/runs/${data.id}/start`, { method: "POST", body: {} }); toast("Workflow started"); render(); };
  }
}

async function renderToday(root) {
  const [home, overview, projects, cost] = await Promise.all([
    quiet("/api/agent/home"),
    quiet("/api/overview"),
    quiet("/api/search-intelligence/projects"),
    quiet("/api/cloud/cost"),
  ]);
  if (home && home.needs_connections) {
    root.innerHTML = `<h1>Welcome to Revenue360</h1>
      <p class="sub">Before we build anything, connect the tools you want Revenue360 to operate.</p>
      <p><button class="btn" id="go-connect" type="button">Open accounts</button></p>`;
    $("go-connect").onclick = () => go("accounts");
    return;
  }
  const attention = [];
  if (overview && overview.leads) attention.push([`${overview.leads} leads in the workspace`, "Open leads", "leads", ""]);
  (overview && overview.activity || []).slice(0, 2).forEach((item) => attention.push([item.detail || item.type, "Review", "leads", ""]));
  (projects || []).slice(0, 2).forEach((project) => attention.push([`${project.domain || "Website"} search analysis ${project.last_analyzed_at ? "is ready" : "is queued"}`, "Investigate", "search", project.id]));
  (cost && cost.plans || []).slice(0, 1).forEach((plan) => attention.push([`${plan.provider || "Cloud"} architecture estimate ${plan.cost && plan.cost.monthly_estimate != null ? `$${plan.cost.monthly_estimate}/mo` : "ready"}`, "Review", "cloud", plan.id]));
  const company = (home && home.company) || {};
  root.innerHTML = `
    <div class="page-head">
      <div>
        <h1>Today</h1>
        <p class="sub">${escapeHtml(company.company_name || "Your workspace")}${home ? ` · ${home.leads || 0} leads · ${home.connected || 0} accounts connected` : ""}</p>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h2>Needs attention</h2><span class="note">${attention.length} item${attention.length === 1 ? "" : "s"}</span></div>
      <div class="attention">
        ${attention.length ? attention.map(([label, action, page, id]) => `<div class="attention-row"><div><strong>${escapeHtml(label)}</strong></div><button class="btn ghost" data-go="${page}" data-id="${escapeHtml(id)}" type="button">${escapeHtml(action)}</button></div>`).join("") : `<p class="meta">Nothing queued. Analyze a website, estimate an architecture, or describe an outcome below.</p>`}
      </div>
    </div>
    <div class="card">
      <h2>Active work</h2>
      ${(home && home.runs || []).length ? home.runs.map((run) => `<div class="work-row"><span>${escapeHtml(run.title || "Untitled")} · ${escapeHtml(run.phase || run.status || "")}</span><button class="btn ghost" data-run="${escapeHtml(run.id)}" type="button">Open</button></div>`).join("") : `<p class="meta">No workflows yet.</p>`}
    </div>
    <div class="card">
      <h2>Quick action</h2>
      <div class="quick-grid">
        <button class="btn ghost" data-go="leads" type="button">Find leads</button>
        <button class="btn ghost" data-go="search" type="button">Improve search</button>
        <button class="btn ghost" data-go="cloud" type="button">Review cloud</button>
        <button class="btn ghost" data-go="campaigns" type="button">Create campaign</button>
      </div>
      <label for="goal-box">Or describe the outcome</label>
      <input id="goal-box" placeholder="Follow up new SaaS leads on WhatsApp" />
      <p><button class="btn" id="start-goal" type="button">Start</button></p>
    </div>`;
  root.querySelectorAll("[data-go]").forEach((button) => {
    button.onclick = () => {
      if (button.dataset.go === "search" && button.dataset.id) siState.projectId = button.dataset.id;
      if (button.dataset.go === "cloud" && button.dataset.id) {
        cloudState.planId = button.dataset.id;
        cloudState.step = "result";
      }
      go(button.dataset.go);
    };
  });
  root.querySelectorAll("[data-run]").forEach((button) => {
    button.onclick = () => { state.runId = button.dataset.run; go("work"); };
  });
  $("start-goal").onclick = async () => {
    const message = $("goal-box").value.trim() || "Grow my business";
    const res = await api("/api/agent/chat", { method: "POST", body: { message } });
    state.runId = res.id;
    state.page = "work";
    paintNav();
    render();
    $("agent-panel") && $("agent-panel").classList.remove("hide");
    drawAgent(res);
  };
}

async function renderWork(root) {
  const runs = await api("/api/agent/runs");
  const current = state.runId ? await api(`/api/agent/runs/${state.runId}`).catch(() => null) : null;
  const wf = current?.workflow || {};
  root.innerHTML = `<h1>Work</h1>
    <p class="sub">Running systems. Details stay underneath.</p>
    <div class="card">${(runs || []).map((r) => `<div class="work-row"><span>${escapeHtml(r.title || "Untitled")} · ${escapeHtml(r.phase)}</span><button class="btn ghost" data-run="${escapeHtml(r.id)}" type="button">Open</button></div>`).join("") || "No work yet."}</div>
    ${current ? `<div class="card">
      <h2>${escapeHtml(current.title)}</h2>
      <p class="sub">${escapeHtml(current.phase)} · ${escapeHtml(current.status)}</p>
      <table>${(wf.steps || []).map((s) => `<tr><td>${escapeHtml(s.tool)}</td><td>${escapeHtml(s.status)}</td></tr>`).join("")}</table>
      <p>${current.phase === "confirmation" ? `<button class="btn" id="start-run" type="button">Review and start</button>` : `<button class="btn ghost" id="pause-run" type="button">Pause</button>`}</p>
    </div>` : ""}`;
  root.querySelectorAll("[data-run]").forEach((btn) => {
    btn.onclick = () => { state.runId = btn.dataset.run; render(); };
  });
  const start = $("start-run");
  if (start) start.onclick = async () => { await api(`/api/agent/runs/${current.id}/start`, { method: "POST", body: {} }); toast("Started"); render(); };
  const pause = $("pause-run");
  if (pause) pause.onclick = async () => { await api(`/api/agent/runs/${current.id}/pause`, { method: "POST", body: {} }); toast("Paused"); render(); };
}

async function renderConnections(root) {
  const [home, settings] = await Promise.all([api("/api/agent/home"), api("/api/settings")]);
  void settings;
  const connected = new Set((home.accounts || []).filter((a) => a.status === "connected").map((a) => a.platform));
  const groups = {
    Communication: ["gmail", "whatsapp", "sms"],
    Social: ["instagram", "facebook", "threads", "linkedin", "x", "tiktok", "youtube"],
    Advertising: ["meta_ads", "google_ads"],
    Content: ["canva", "veed"],
  };
  const labels = { gmail: "Gmail", whatsapp: "WhatsApp", sms: "SMS", instagram: "Instagram", facebook: "Facebook", threads: "Threads", linkedin: "LinkedIn", x: "X", tiktok: "TikTok", youtube: "YouTube", meta_ads: "Meta Ads", google_ads: "Google Ads", canva: "Canva", veed: "VEED" };
  root.innerHTML = `<h1>Accounts</h1>
    <p class="sub">Connect with the provider. Advanced credentials stay hidden unless you need them.</p>
    ${Object.entries(groups).map(([title, items]) => `<div class="card"><h3>${title}</h3>
      ${items.map((p) => `<div class="row"><span>${connected.has(p) || (p === "gmail" && connected.has("email")) ? "✓" : "○"} ${labels[p]}</span>
        <span><button class="btn" data-connect="${p}" type="button">Connect</button> <button class="btn ghost" data-advanced="${p}" type="button">Advanced</button></span></div>`).join("")}
    </div>`).join("")}
    <div class="card"><h3>AI Engine</h3><p>Status: Ready ✓</p><p class="sub">Automatic model selection. Simple tasks use a cheap model. Planning uses a stronger model. Keys stay on the server.</p></div>
    <div class="card hide" id="advanced-box"></div>`;
  root.querySelectorAll("[data-connect]").forEach((btn) => {
    btn.onclick = async () => {
      const res = await api("/api/agent/connect/oauth", { method: "POST", body: { provider: btn.dataset.connect } });
      if (res.url) window.location.href = res.url;
      else toast(res.message || "Use Advanced setup for this provider");
    };
  });
  root.querySelectorAll("[data-advanced]").forEach((btn) => {
    btn.onclick = () => renderAccounts(root);
  });
}

async function renderContent(root) {
  root.innerHTML = `<h1>Content</h1>
    <p class="sub">Built-in image and video work without Canva or VEED. Connect those only if you want your own accounts.</p>
    <div class="card">
      <label>What should we make?</label>
      <textarea id="content-prompt" placeholder="15s reel for Indian salons about our SaaS"></textarea>
      <div class="checks">
        <label><input type="radio" name="content-kind" value="image" checked /> Image</label>
        <label><input type="radio" name="content-kind" value="video" /> Video</label>
        <label><input type="radio" name="content-kind" value="workflow" /> Workflow</label>
      </div>
      <p><button class="btn" id="content-go" type="button">Create</button></p>
      <div id="content-out"></div>
    </div>`;
  $("content-go").onclick = async () => {
    const kind = root.querySelector("input[name=content-kind]:checked").value;
    const out = $("content-out");
    out.innerHTML = "<p class='sub'>Creating…</p>";
    try {
      const res = await api("/api/agent/content", { method: "POST", body: { kind, prompt: $("content-prompt").value } });
      if (kind === "workflow") out.innerHTML = `<pre>${escapeHtml(JSON.stringify(res.workflow || res, null, 2))}</pre>`;
      else if (res.url) {
        out.innerHTML = kind === "image"
          ? `<img src="${escapeHtml(res.url)}" alt="Generated" style="max-width:100%" /><p class="sub">${escapeHtml(res.provider)}</p>`
          : `<video src="${escapeHtml(res.url)}" controls style="max-width:100%"></video><p class="sub">${escapeHtml(res.note || res.provider)}</p>`;
      } else out.innerHTML = `<p class="sub">${escapeHtml(res.note || "No media URL")}</p>`;
    } catch (error) {
      out.innerHTML = `<p class="sub">${escapeHtml(error.message)}</p>`;
    }
  };
}

async function renderCompany(root) {
  const data = await api("/api/company");
  const p = data.profile || {};
  root.innerHTML = `<h1>Company</h1>
    <p class="sub">AI only writes well if it knows what you sell. Add a short brief and upload price lists or brochures.</p>
    <div class="card">
      <div class="row">
        <div><label>Company name</label><input id="c-name" value="${escapeHtml(p.company_name || "")}" /></div>
        <div><label>Website</label><input id="c-web" value="${escapeHtml(p.website || "")}" /></div>
      </div>
      <div class="row">
        <div><label>Industry</label><input id="c-ind" value="${escapeHtml(p.industry || "")}" /></div>
        <div><label>First-reply style</label>
          <select id="c-mode"><option value="mine" ${p.first_reply_mode !== "ai" ? "selected" : ""}>I write first replies</option>
          <option value="ai" ${p.first_reply_mode === "ai" ? "selected" : ""}>AI may write first replies</option></select>
        </div>
      </div>
      <label>What you sell</label><textarea id="c-offer">${escapeHtml(p.offer || "")}</textarea>
      <label>Brand voice</label><textarea id="c-voice">${escapeHtml(p.brand_voice || "")}</textarea>
      <p><button class="btn" id="save-co" type="button">Save company</button></p>
      <label>Upload PDF, TXT or CSV</label><input type="file" id="doc" />
      <table>${(data.documents || []).map((d) => `<tr><td>${escapeHtml(d.filename)}</td><td>${d.chars || 0} chars extracted</td></tr>`).join("")}</table>
    </div>`;
  $("save-co").onclick = async () => {
    await api("/api/company", { method: "POST", body: {
      company_name: $("c-name").value, website: $("c-web").value, industry: $("c-ind").value,
      offer: $("c-offer").value, brand_voice: $("c-voice").value, first_reply_mode: $("c-mode").value
    }});
    toast("Company saved");
  };
  $("doc").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    const res = await api("/api/company/documents", { method: "POST", body: fd });
    toast(`Uploaded ${res.filename}`);
    render();
  };
}

async function renderOverview(root) {
  const [data, accounts] = await Promise.all([api("/api/overview"), api("/api/accounts")]);
  void accounts;
  if ($("mode-badge")) {
    const live = Object.values(data.modes || {}).filter((m) => m === "live").length;
    $("mode-badge").textContent = live ? `${live}/3 channels live` : "Sandbox";
    $("mode-badge").classList.toggle("live", live > 0);
  }
  root.innerHTML = `
    <h1>Analytics</h1>
    <div class="metrics">
      <div class="card"><span class="sub">Leads</span><b>${data.leads}</b></div>
      <div class="card"><span class="sub">Campaigns</span><b>${data.campaigns}</b></div>
      <div class="card"><span class="sub">Messages</span><b>${data.messages}</b></div>
      <div class="card"><span class="sub">Opens</span><b>${data.opens}</b></div>
      <div class="card"><span class="sub">Clicks</span><b>${data.clicks}</b></div>
    </div>
    <p><button class="btn" id="run-due" type="button">Send due follow-ups now</button></p>
    <div class="card"><table>${(data.activity || []).map((r) => `<tr><td>${escapeHtml(r.created_at)}</td><td>${escapeHtml(r.type)}</td><td>${escapeHtml(r.detail || "")}</td></tr>`).join("")}</table></div>`;
  $("run-due").onclick = async () => { toast(await api("/api/worker/run", { method: "POST", body: {} })); };
}