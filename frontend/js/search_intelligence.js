const siState = { projectId: "", tab: "overview", timer: 0 };

function siParse(value, fallback) {
  if (value == null || value === "") return fallback;
  if (typeof value !== "string") return value;
  try { return JSON.parse(value); } catch { return fallback; }
}

function siPhase(run) {
  return String((run && (run.phase || run.status)) || "not started");
}

function siPressure(findings) {
  const rows = findings || [];
  if (!rows.length) return { label: "No open findings", width: 8 };
  const impact = rows.reduce((sum, row) => sum + Number(row.impact_score || 0), 0) / rows.length;
  if (impact >= 70 || rows.length >= 12) return { label: "High issue pressure", width: 84 };
  if (impact >= 40 || rows.length >= 5) return { label: "Medium issue pressure", width: 56 };
  return { label: "Low issue pressure", width: 32 };
}

function siClass(value) {
  const kind = String(value || "USER_APPROVAL");
  const map = {
    SAFE_AUTOMATION: ["safe", "Safe to automate"],
    USER_APPROVAL: ["approval", "Needs your approval"],
    MANUAL_ACTION: ["manual", "Manual action"],
    INSUFFICIENT_DATA: ["manual", "Not enough evidence"],
  };
  return map[kind] || map.USER_APPROVAL;
}

function closeDrawer() {
  document.querySelectorAll(".drawer, .drawer-scrim").forEach((node) => node.remove());
}

function openDrawer(html) {
  closeDrawer();
  const scrim = document.createElement("div");
  scrim.className = "drawer-scrim";
  const drawer = document.createElement("aside");
  drawer.className = "drawer";
  drawer.setAttribute("role", "dialog");
  drawer.setAttribute("aria-modal", "true");
  drawer.innerHTML = html;
  scrim.onclick = closeDrawer;
  document.body.append(scrim, drawer);
  const close = drawer.querySelector("[data-close]");
  if (close) close.onclick = closeDrawer;
  const onKey = (event) => {
    if (event.key === "Escape") {
      closeDrawer();
      document.removeEventListener("keydown", onKey);
    }
  };
  document.addEventListener("keydown", onKey);
  return drawer;
}

function stopSearchPoll() {
  if (siState.timer) clearInterval(siState.timer);
  siState.timer = 0;
}

async function renderSearch(root) {
  stopSearchPoll();
  closeDrawer();
  if (siState.projectId) return renderSearchProject(root, siState.projectId);
  return renderSearchHome(root);
}

async function renderSearchHome(root) {
  const projects = await api("/api/search-intelligence/projects");
  root.innerHTML = `
    <div class="page-head">
      <div>
        <h1>Search</h1>
        <p class="sub">How this website is discovered, understood, and cited. Findings come from the crawl. Rankings, traffic, and backlinks are not invented.</p>
      </div>
      <button class="btn" id="si-new" type="button">Analyze website</button>
    </div>
    <div class="card" id="si-form-card">
      <label for="si-url">Website</label>
      <input id="si-url" type="url" placeholder="https://example.com" />
      <label for="si-name">Business name, if the site does not say it</label>
      <input id="si-name" placeholder="Optional" />
      <details class="advanced">
        <summary>Country or audience, optional</summary>
        <label for="si-country">Target country</label>
        <input id="si-country" placeholder="India" />
      </details>
      <p><button class="btn" id="si-start" type="button">Start analysis</button></p>
      <p class="note">The crawl respects robots.txt, stores page evidence, then proposes actions. Safe changes are plans until you approve them. The live site is not edited from here.</p>
    </div>
    <div class="card">
      <div class="card-head"><h2>Websites</h2><span class="note">${projects.length} project${projects.length === 1 ? "" : "s"}</span></div>
      ${projects.length ? projects.map((project) => `
        <div class="project-row">
          <div>
            <strong>${escapeHtml(project.domain || project.website_url)}</strong>
            <span class="meta">${escapeHtml(project.business_name || "Business name not set")} · ${escapeHtml(project.status || "active")} · last analyzed ${escapeHtml(project.last_analyzed_at || "not yet")}</span>
          </div>
          <button class="btn ghost" data-project="${escapeHtml(project.id)}" type="button">Open</button>
        </div>`).join("") : `<p class="empty meta">No website yet. Add a URL. The first screen after that is evidence, not a configuration form.</p>`}
    </div>`;
  $("si-start").onclick = () => startSearch(root);
  root.querySelectorAll("[data-project]").forEach((button) => {
    button.onclick = () => {
      siState.projectId = button.dataset.project;
      siState.tab = "overview";
      renderSearch(root);
    };
  });
}

async function startSearch(root) {
  const website = $("si-url").value.trim();
  if (!website) return toast("Add a website URL");
  const button = $("si-start");
  button.disabled = true;
  try {
    const created = await api("/api/search-intelligence/projects", {
      method: "POST",
      body: {
        website_url: website,
        business_name: $("si-name").value.trim(),
        target_country: $("si-country").value.trim(),
      },
    });
    siState.projectId = created.id;
    siState.tab = "overview";
    toast("Analysis queued");
    renderSearch(root);
  } catch (error) {
    button.disabled = false;
    toast(error.message);
  }
}

async function renderSearchProject(root, projectId) {
  const [project, findings, opportunities, actions] = await Promise.all([
    api(`/api/search-intelligence/projects/${projectId}`),
    api(`/api/search-intelligence/projects/${projectId}/findings`),
    api(`/api/search-intelligence/projects/${projectId}/opportunities`),
    api(`/api/search-intelligence/projects/${projectId}/actions`),
  ]);
  const run = project.run || {};
  const progress = run.progress || {};
  const phase = siPhase(run);
  const running = !["completed", "failed"].includes(phase);
  const pressure = siPressure(findings.seo);
  const business = project.business || {};
  const products = siParse(business.products, []);
  root.innerHTML = `
    <div class="page-head">
      <div>
        <button class="btn ghost" id="si-back" type="button">All websites</button>
        <h1>${escapeHtml(project.project.domain || project.project.website_url)}</h1>
        <p class="sub">${escapeHtml(project.project.website_url)} · ${escapeHtml(phase)}${progress.pages_crawled != null ? ` · ${escapeHtml(progress.pages_crawled)} pages crawled` : ""}</p>
      </div>
      <button class="btn" id="si-rerun" type="button">Re-analyze</button>
    </div>
    <div class="tabs" role="tablist">
      ${["overview", "technical", "content", "queries", "competitors", "ai"].map((tab) => `<button type="button" data-tab="${tab}" class="${siState.tab === tab ? "active" : ""}">${tab === "ai" ? "AI visibility" : tab[0].toUpperCase() + tab.slice(1)}</button>`).join("")}
    </div>
    <div id="si-panel"></div>`;
  $("si-back").onclick = () => { siState.projectId = ""; renderSearch(root); };
  $("si-rerun").onclick = async () => {
    await api(`/api/search-intelligence/projects/${projectId}/analyze`, { method: "POST", body: {} });
    toast("Re-analysis queued");
    renderSearch(root);
  };
  root.querySelectorAll("[data-tab]").forEach((button) => {
    button.onclick = () => { siState.tab = button.dataset.tab; renderSearch(root); };
  });
  const panel = $("si-panel");
  if (siState.tab === "overview") panel.innerHTML = searchOverview(project, findings, opportunities, actions, pressure, business, products);
  if (siState.tab === "technical") panel.innerHTML = searchFindings(findings.seo, "technical");
  if (siState.tab === "content") panel.innerHTML = searchFindings(findings.seo, "content");
  if (siState.tab === "queries") panel.innerHTML = searchQueries(findings.aeo_questions, opportunities);
  if (siState.tab === "competitors") panel.innerHTML = searchCompetitors(project.project, opportunities);
  if (siState.tab === "ai") panel.innerHTML = searchAi(findings);
  bindOpportunityClicks(panel, opportunities, actions);
  if (running) {
    siState.timer = setInterval(() => renderSearch(root), 5000);
  }
}

function searchOverview(project, findings, opportunities, actions, pressure, business, products) {
  const counts = project.counts || {};
  const top = opportunities.slice(0, 5);
  return `
    <div class="split">
      <div>
        <div class="card">
          <div class="card-head"><h2>Issue pressure</h2><span class="pill">${escapeHtml(pressure.label)}</span></div>
          <div class="bar" aria-hidden="true"><span style="width:${pressure.width}%"></span></div>
          <p class="note">Derived from open findings on this crawl. This is not a ranking, visibility score, or traffic estimate.</p>
          <div class="metrics">
            <div><span class="meta">Findings</span><b>${counts.findings || 0}</b></div>
            <div><span class="meta">Opportunities</span><b>${counts.opportunities || 0}</b></div>
            <div><span class="meta">Actions</span><b>${counts.actions || 0}</b></div>
          </div>
        </div>
        <div class="card">
          <h2>Next actions</h2>
          ${top.length ? top.map((item) => opportunityRow(item, actions)).join("") : `<p class="meta">No opportunities yet. They appear after the crawl finishes.</p>`}
        </div>
      </div>
      <div class="card">
        <h2>What the crawl understood</h2>
        <p><strong>${escapeHtml(business.company_name || project.project.business_name || "Not identified")}</strong></p>
        <p class="meta">${escapeHtml(business.industry || project.project.industry || "Industry not stated")} · confidence ${Math.round(Number(business.confidence_score || 0) * 100)}%</p>
        <p class="meta">${escapeHtml(business.description || "No business description extracted yet.")}</p>
        ${products.length ? `<p class="meta">Products: ${products.map((item) => escapeHtml(item)).join(", ")}</p>` : ""}
        <p class="note">${escapeHtml(business.evidence || "Evidence is attached when the analyzer can point at a page.")}</p>
      </div>
    </div>`;
}

function opportunityRow(item, actions) {
  const action = actions.find((row) => row.opportunity_id === item.id) || {};
  const [pill, label] = siClass(action.classification);
  return `
    <div class="opp">
      <div>
        <strong>${escapeHtml(item.title)}</strong>
        <span class="meta">${escapeHtml(item.root_cause || "evidence")} · impact ${escapeHtml(item.impact_score || 0)} · effort ${escapeHtml(item.effort_score || "—")}</span>
      </div>
      <div>
        <span class="pill ${pill}">${label}</span>
        <button class="btn ghost" data-opp="${escapeHtml(item.id)}" type="button">Open</button>
      </div>
    </div>`;
}

function searchFindings(rows, mode) {
  const contentCodes = new Set(["thin_content", "missing_title", "short_title", "duplicate_title", "missing_meta_description", "missing_h1", "missing_entity_clarity"]);
  const filtered = (rows || []).filter((row) => mode === "content" ? contentCodes.has(row.issue_code) : !contentCodes.has(row.issue_code));
  if (!filtered.length) return `<div class="card"><p class="meta">No ${mode} findings on the latest crawl.</p></div>`;
  return `<div class="card"><table><thead><tr><th>Finding</th><th>Severity</th><th>Evidence</th></tr></thead><tbody>
    ${filtered.map((row) => `<tr><td>${escapeHtml(row.title)}</td><td>${escapeHtml(row.severity)}</td><td>${escapeHtml(row.evidence || row.description || "")}</td></tr>`).join("")}
  </tbody></table></div>`;
}

function searchQueries(questions, opportunities) {
  const queries = (opportunities || []).flatMap((item) => siParse(item.affected_queries, []));
  const unique = [...new Set(queries.filter(Boolean))];
  return `<div class="card">
    <h2>Questions worth answering</h2>
    ${(questions || []).length ? (questions || []).map((item) => `<div class="opp"><div><strong>${escapeHtml(item.query)}</strong><span class="meta">${escapeHtml(item.intent || "unmeasured")} · ${escapeHtml(item.status || "unmeasured")}</span></div></div>`).join("") : `<p class="meta">No AI questions stored for this run.</p>`}
    <h3>Query links on opportunities</h3>
    ${unique.length ? `<p class="meta">${unique.map((item) => escapeHtml(item)).join(" · ")}</p>` : `<p class="meta">No query was attached to an opportunity. Search volume is not estimated.</p>`}
  </div>`;
}

function searchCompetitors(project, opportunities) {
  const seed = siParse(project.competitors_seed, []);
  const found = (opportunities || []).flatMap((item) => siParse(item.affected_competitors, []));
  const rows = [...new Set([...seed, ...found].filter(Boolean))];
  return `<div class="card"><h2>Competitor hypotheses</h2>
    ${rows.length ? rows.map((item) => `<div class="opp"><div><strong>${escapeHtml(item)}</strong><span class="meta">Hypothesis or user seed. Not a measured ranking.</span></div></div>`).join("") : `<p class="meta">No competitor was observed or seeded. This tab stays empty rather than guessing domains.</p>`}
  </div>`;
}

function searchAi(findings) {
  const geo = findings.geo || [];
  const questions = findings.aeo_questions || [];
  return `<div class="card"><h2>AI citation readiness</h2>
    <p class="note">Mention and citation counts stay unmeasured until an observation exists. This list is eligibility evidence, not visibility.</p>
    ${geo.length ? geo.map((item) => `<div class="opp"><div><strong>${escapeHtml(item.title)}</strong><span class="meta">${escapeHtml(item.description || "")}</span></div><span class="pill">${escapeHtml(item.code)}</span></div>`).join("") : `<p class="meta">No GEO findings stored.</p>`}
    <h3>Questions</h3>
    ${questions.length ? questions.map((item) => `<p class="meta">${escapeHtml(item.query)} · ${escapeHtml(item.status || "unmeasured")}</p>`).join("") : `<p class="meta">No questions stored.</p>`}
  </div>`;
}

function bindOpportunityClicks(panel, opportunities, actions) {
  panel.querySelectorAll("[data-opp]").forEach((button) => {
    button.onclick = () => {
      const item = opportunities.find((row) => row.id === button.dataset.opp);
      const action = actions.find((row) => row.opportunity_id === item.id) || {};
      const [pill, label] = siClass(action.classification);
      const drawer = openDrawer(`
        <div class="drawer-head"><h2>${escapeHtml(item.title)}</h2><button class="icon-btn" data-close type="button">Close</button></div>
        <span class="pill ${pill}">${label}</span>
        <p>${escapeHtml(item.description || "")}</p>
        <p class="meta">Impact ${escapeHtml(item.impact_score || 0)} · confidence ${escapeHtml(item.confidence_score || 0)} · effort ${escapeHtml(item.effort_score || "—")}</p>
        <h3>Why this is here</h3>
        <p class="meta">${escapeHtml(item.root_cause || "The rule matched crawled evidence.")}</p>
        <h3>Evidence</h3>
        <p class="meta">${escapeHtml(item.evidence || "No page excerpt stored on this opportunity.")}</p>
        <h3>Recommended action</h3>
        <p class="meta">${escapeHtml(action.title || "Review the finding.")} ${escapeHtml(action.description || "")}</p>
        <div class="actions" id="si-drawer-actions"></div>
        <p class="note" id="si-job"></p>`);
      const actionsEl = drawer.querySelector("#si-drawer-actions");
      if (action.id && action.classification !== "INSUFFICIENT_DATA") {
        const apply = document.createElement("button");
        apply.className = "btn";
        apply.textContent = action.classification === "MANUAL_ACTION" ? "Show instructions" : "Prepare action";
        apply.onclick = async () => {
          const job = await api(`/api/search-intelligence/actions/${action.id}/execute`, { method: "POST", body: {} });
          drawer.querySelector("#si-job").textContent = job.status === "ready_for_review"
            ? "Patch plan only. The live site was not modified."
            : `Status: ${job.status}`;
        };
        actionsEl.append(apply);
      }
      if (action.id) {
        const ignore = document.createElement("button");
        ignore.className = "btn ghost";
        ignore.textContent = "Not applicable";
        ignore.onclick = async () => {
          await api(`/api/search-intelligence/actions/${action.id}/feedback`, { method: "POST", body: { feedback_type: "NOT_APPLICABLE" } });
          toast("Marked not applicable");
          closeDrawer();
        };
        actionsEl.append(ignore);
      }
    };
  });
}