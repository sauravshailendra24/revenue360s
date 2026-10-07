const cloudState = {
  step: 0,
  answers: { kind: "api", traffic: "5", region: "india", budget: "50", availability: "99.9", managed: true },
  planId: "",
};

const CLOUD_STEPS = [
  {
    key: "kind",
    prompt: "What are you deploying?",
    options: [
      ["api", "API", "Request/response service"],
      ["web", "Web application", "Same path as an API for this catalog"],
      ["worker", "Background workers", "Still estimated as the app tier"],
      ["data", "Data pipeline", "Catalog currently prices the app and database"],
    ],
  },
  {
    key: "traffic",
    prompt: "How much peak traffic?",
    options: [
      ["5", "Small", "About 5 requests/second"],
      ["30", "Growing", "About 30 requests/second"],
      ["100", "High", "About 100 requests/second"],
    ],
  },
  {
    key: "region",
    prompt: "Where are the users?",
    options: [
      ["india", "India", "Mumbai or Central India"],
      ["singapore", "Singapore", "Closest Asia catalog region"],
      ["us", "United States", "East US / Iowa"],
    ],
  },
  {
    key: "budget",
    prompt: "Monthly ceiling, USD",
    options: [["25", "$25", "Tight"], ["50", "$50", "Typical start"], ["100", "$100", "Room to grow"], ["250", "$250", "Higher headroom"]],
  },
];

function cloudMoney(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `$${number.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function cloudBurden(value) {
  const number = Number(value);
  if (number <= 0.3) return "Low";
  if (number <= 0.5) return "Medium";
  return "High";
}

function cloudSource(sources) {
  const list = sources || [];
  if (!list.length) return "missing";
  if (list.includes("azure_retail")) return "catalog";
  if (list.every((item) => item === "seed" || item === "missing")) return "seed";
  return list.join(", ");
}

function architectureLabel(item) {
  if (!item) return "Architecture";
  const parts = (item.components || []).map((component) => component.service);
  return parts.join(" + ") || item.name || "Architecture";
}

async function renderCloud(root) {
  closeDrawer();
  if (cloudState.planId && cloudState.step === "result") return renderCloudPlan(root, cloudState.planId);
  if (cloudState.step === "wizard") return renderCloudWizard(root);
  return renderCloudHome(root);
}

async function renderCloudHome(root) {
  const [plans, cost, accounts] = await Promise.all([
    api("/api/cloud/plans"),
    api("/api/cloud/cost"),
    api("/api/cloud/accounts"),
  ]);
  const estimates = new Map((cost.plans || []).map((plan) => [plan.id, plan.cost || {}]));
  root.innerHTML = `
    <div class="page-head">
      <div>
        <h1>Cloud</h1>
        <p class="sub">Plan the infrastructure before spending. Estimates use the price catalog. Seed rows are not an invoice.</p>
      </div>
      <button class="btn" id="cloud-new" type="button">New architecture</button>
    </div>
    <div class="card">
      <div class="card-head"><h2>Architectures</h2><span class="note">${plans.length} plan${plans.length === 1 ? "" : "s"}</span></div>
      ${plans.length ? plans.map((plan) => {
        const estimate = estimates.get(plan.id) || {};
        return `<div class="project-row">
          <div>
            <strong>${escapeHtml(plan.architecture_id || "Awaiting answers")}</strong>
            <span class="meta">${escapeHtml(plan.provider || "provider not chosen")} · ${escapeHtml(plan.status || "draft")} · ${cloudMoney(estimate.monthly_estimate)} / month · ${escapeHtml(cloudSource(estimate.sources))}</span>
          </div>
          <button class="btn ghost" data-plan="${escapeHtml(plan.id)}" type="button">Review</button>
        </div>`;
      }).join("") : `<p class="empty meta">No architecture yet. Describe what you are deploying. Nothing is created in a cloud account from this screen.</p>`}
    </div>
    <div class="card">
      <h2>Cost note</h2>
      <p class="meta">${escapeHtml(cost.note || "Estimates use cloud_prices. source=seed is not an invoice.")}</p>
    </div>
    <details class="card advanced">
      <summary>Cloud account, only when you are ready to verify</summary>
      <p class="note">Credentials stay encrypted. Apply stays blocked unless the server flag is on, and a verified account is required before any deploy request.</p>
      ${(accounts || []).map((account) => `<div class="project-row"><div><strong>${escapeHtml(account.account_name)}</strong><span class="meta">${escapeHtml(account.provider)} · ${escapeHtml(account.status)}</span></div><button class="btn ghost" data-verify="${escapeHtml(account.id)}" type="button">Verify</button></div>`).join("") || `<p class="meta">No account connected.</p>`}
      <div class="row">
        <div><label>Provider</label><select id="cloud-provider"><option>aws</option><option>gcp</option><option>azure</option></select></div>
        <div><label>Account identifier</label><input id="cloud-account-id" placeholder="Account or subscription id" /></div>
      </div>
      <p><button class="btn ghost" id="cloud-save-account" type="button">Save account</button></p>
    </details>`;
  $("cloud-new").onclick = () => { cloudState.step = "wizard"; renderCloud(root); };
  root.querySelectorAll("[data-plan]").forEach((button) => {
    button.onclick = () => { cloudState.planId = button.dataset.plan; cloudState.step = "result"; renderCloud(root); };
  });
  root.querySelectorAll("[data-verify]").forEach((button) => {
    button.onclick = async () => {
      const result = await api(`/api/cloud/accounts/${button.dataset.verify}/verify`, { method: "POST", body: {} });
      toast(result.detail || result.status);
      renderCloud(root);
    };
  });
  $("cloud-save-account").onclick = async () => {
    await api("/api/cloud/accounts", { method: "POST", body: { provider: $("cloud-provider").value, account_identifier: $("cloud-account-id").value, account_name: $("cloud-account-id").value } });
    toast("Account saved");
    renderCloud(root);
  };
}

function renderCloudWizard(root) {
  const step = CLOUD_STEPS[cloudState.answers._index || 0];
  const index = cloudState.answers._index || 0;
  root.innerHTML = `
    <div class="page-head">
      <div>
        <button class="btn ghost" id="cloud-back" type="button">Architectures</button>
        <h1>New architecture</h1>
        <p class="sub">Step ${index + 1} of ${CLOUD_STEPS.length}. ${escapeHtml(step.prompt)}</p>
      </div>
    </div>
    <div class="card"><div class="choice-grid" id="cloud-choices"></div></div>`;
  $("cloud-back").onclick = () => { cloudState.step = 0; renderCloud(root); };
  const wrap = $("cloud-choices");
  step.options.forEach(([value, label, detail]) => {
    const button = document.createElement("button");
    button.className = "choice";
    button.type = "button";
    button.innerHTML = `<b>${escapeHtml(label)}</b><small>${escapeHtml(detail)}</small>`;
    button.onclick = async () => {
      cloudState.answers[step.key] = value;
      if (index + 1 < CLOUD_STEPS.length) {
        cloudState.answers._index = index + 1;
        renderCloudWizard(root);
        return;
      }
      await submitCloud(root);
    };
    wrap.append(button);
  });
}

async function submitCloud(root) {
  root.innerHTML = `<div class="card"><p class="meta">Comparing catalog architectures…</p></div>`;
  const answers = cloudState.answers;
  const result = await api("/api/cloud/analyze", {
    method: "POST",
    body: {
      message: `Deploy a ${answers.kind} for users in ${answers.region}. Peak ${answers.traffic} rps. Budget ${answers.budget} USD.`,
      requirements: {
        application: { framework: "fastapi" },
        database: { type: "postgresql", existing: false },
        traffic: { peak_rps: Number(answers.traffic) },
        region: { primary: answers.region, preferred: answers.region },
        availability: { target: answers.availability },
        budget: { monthly_max: Number(answers.budget) },
        preferences: { managed_services: true, containerized: true, avoid_kubernetes: true },
        security: { public_database: false, private_network: true },
      },
    },
  });
  cloudState.planId = result.id;
  cloudState.step = "result";
  if (result.ask) {
    renderCloudAsk(root, result);
    return;
  }
  renderCloudResult(root, result);
}

function renderCloudAsk(root, plan) {
  const ask = plan.ask || {};
  root.innerHTML = `
    <div class="page-head"><div><h1>One more fact</h1><p class="sub">${escapeHtml(ask.prompt || "The catalog needs another answer.")}</p></div></div>
    <div class="card"><div class="choice-grid" id="cloud-ask"></div></div>`;
  const wrap = $("cloud-ask");
  (ask.options || []).forEach((option) => {
    const button = document.createElement("button");
    button.className = "choice";
    button.type = "button";
    button.textContent = option;
    button.onclick = async () => {
      const next = await api("/api/cloud/answer", { method: "POST", body: { plan_id: plan.id, field: ask.field, value: option } });
      cloudState.planId = next.id;
      if (next.ask) renderCloudAsk(root, next);
      else renderCloudResult(root, next);
    };
    wrap.append(button);
  });
}

async function renderCloudPlan(root, planId) {
  const plan = await api(`/api/cloud/plans/${planId}`);
  if (plan.ask) return renderCloudAsk(root, plan);
  renderCloudResult(root, plan);
}

function renderCloudResult(root, plan) {
  const chosen = (plan.architecture || {}).chosen || {};
  const alternatives = (plan.architecture || {}).alternatives || [];
  const security = plan.security || {};
  const findings = security.findings || [];
  root.innerHTML = `
    <div class="page-head">
      <div>
        <button class="btn ghost" id="cloud-all" type="button">All architectures</button>
        <h1>${escapeHtml((chosen.provider || plan.provider || "Plan").toUpperCase())}</h1>
        <p class="sub">${escapeHtml(architectureLabel(chosen))} · ${escapeHtml(plan.status || "draft")}</p>
      </div>
      <button class="btn" id="cloud-approve" type="button">Approve architecture</button>
    </div>
    <div class="split">
      <div class="card">
        <div class="card-head"><h2>Recommended</h2><span class="pill ${cloudSource(chosen.price_sources) === "seed" ? "seed" : "safe"}">${escapeHtml(cloudSource(chosen.price_sources || (plan.cost || {}).sources))}</span></div>
        <div class="arch">${(chosen.components || []).map((component, index) => `${index ? `<div class="arch-line"></div>` : ""}<div class="arch-node"><b>${escapeHtml(component.service)}</b><span class="meta">${escapeHtml(component.sku || component.role)} · ${cloudMoney(component.monthly)}</span></div>`).join("")}</div>
        <p><b>${cloudMoney((plan.cost || {}).monthly_estimate || chosen.monthly_estimate)}</b> <span class="meta">/ month estimate</span></p>
        <p class="meta">${escapeHtml(plan.explanation || "Approval is required before any write.")}</p>
      </div>
      <div class="card">
        <h2>Why this one</h2>
        <p class="meta">Ops burden ${cloudBurden(chosen.ops_burden)} · security profile ${Math.round(Number(security.score || 0) * 100)}% · confidence ${Math.round(Number(plan.confidence || 0) <= 1 ? Number(plan.confidence || 0) * 100 : Number(plan.confidence || 0))}%</p>
        <p class="meta">Region ${escapeHtml(chosen.region || "—")} · within budget ${chosen.within_budget === false ? "no" : "yes or not checked"}</p>
        ${findings.length ? findings.map((item) => `<p class="meta">${escapeHtml(item.severity)} · ${escapeHtml(item.evidence)}</p>`).join("") : `<p class="meta">No high findings in the architecture profile.</p>`}
        <p class="note">Decision source: ${escapeHtml((plan.jev || {}).provider || "deterministic")}. Apply is a separate approval and stays blocked while CLOUD_ALLOW_APPLY is off.</p>
      </div>
    </div>
    <div class="card">
      <h2>Compare</h2>
      <table class="compare"><thead><tr><th></th><th>Recommended</th>${alternatives.map((item) => `<th>${escapeHtml(item.provider)}</th>`).join("")}</tr></thead>
        <tbody>
          <tr><td>Estimate</td><td>${cloudMoney(chosen.monthly_estimate)}</td>${alternatives.map((item) => `<td>${cloudMoney(item.monthly_estimate)}</td>`).join("")}</tr>
          <tr><td>Ops burden</td><td>${cloudBurden(chosen.ops_burden)}</td>${alternatives.map((item) => `<td>${cloudBurden(item.ops_burden)}</td>`).join("")}</tr>
          <tr><td>Region</td><td>${escapeHtml(chosen.region || "")}</td>${alternatives.map((item) => `<td>${escapeHtml(item.region || "")}</td>`).join("")}</tr>
          <tr><td>Price source</td><td>${escapeHtml(cloudSource(chosen.price_sources))}</td>${alternatives.map((item) => `<td>${escapeHtml(cloudSource(item.price_sources))}</td>`).join("")}</tr>
          <tr><td></td><td></td>${alternatives.map((item) => `<td><button class="btn ghost" data-provider="${escapeHtml(item.provider)}" type="button">Use this</button></td>`).join("")}</tr>
        </tbody>
      </table>
    </div>
    <div class="card">
      <h2>Approval</h2>
      <p class="meta">Current approval: ${escapeHtml(plan.approval_status || "none")}.</p>
      <div class="actions">
        <button class="btn ghost" id="cloud-plan" type="button">Approve plan</button>
        <button class="btn ghost" id="cloud-terraform" type="button">Review template</button>
        <button class="btn ghost" id="cloud-reject" type="button">Reject</button>
      </div>
      <pre id="cloud-template" class="hide"></pre>
    </div>`;
  $("cloud-all").onclick = () => { cloudState.step = 0; cloudState.planId = ""; renderCloud(root); };
  $("cloud-approve").onclick = () => approveCloud(plan.id, "architecture", root);
  $("cloud-plan").onclick = () => approveCloud(plan.id, "plan", root);
  $("cloud-reject").onclick = async () => {
    await api(`/api/cloud/plans/${plan.id}/reject`, { method: "POST", body: {} });
    toast("Plan returned to draft");
    renderCloudPlan(root, plan.id);
  };
  $("cloud-terraform").onclick = async () => {
    const result = await api(`/api/cloud/plans/${plan.id}/terraform`, { method: "POST", body: {} });
    const box = $("cloud-template");
    box.classList.remove("hide");
    box.textContent = `${result.detail || ""}\n\n${result.terraform || plan.terraform || ""}`;
  };
  root.querySelectorAll("[data-provider]").forEach((button) => {
    button.onclick = async () => {
      const next = await api(`/api/cloud/plans/${plan.id}/modify`, {
        method: "POST",
        body: { requirements: { preferences: { provider: button.dataset.provider } }, message: `Prefer ${button.dataset.provider}` },
      });
      renderCloudResult(root, next);
    };
  });
}

async function approveCloud(planId, step, root) {
  await api(`/api/cloud/plans/${planId}/approve`, { method: "POST", body: { step } });
  toast(`${step} approved`);
  renderCloudPlan(root, planId);
}