async function renderTemplates(root) {
  const rows = await api("/api/templates");
  root.innerHTML = `<h1>Templates</h1>
    <p class="sub">Save reusable copy once. Sequences and composers can reuse {{name}}, {{email}}, {{phone}}.</p>
    <div class="card">
      <label>Template name</label><input id="t-name" />
      <div class="row"><div><label>Channel</label>
        <select id="t-ch"><option>email</option><option>sms</option><option>whatsapp</option><option value="instagram_dm">Instagram comment DM</option><option value="messenger">Facebook Messenger</option><option value="threads_reply">Threads public reply</option></select></div>
        <div><label>Subject (email)</label><input id="t-subj" /></div></div>
      <label>Body</label><textarea id="t-body"></textarea>
      <p><button class="btn" id="save-tpl">Save template</button></p>
    </div>
    <div class="card"><table><tr><th>Name</th><th>Channel</th><th>Subject</th><th>Body</th></tr>
      ${rows.map((r) => `<tr><td>${escapeHtml(r.name)}</td><td>${escapeHtml(r.channel)}</td><td>${escapeHtml(r.subject)}</td><td>${escapeHtml((r.body || "").slice(0, 80))}</td></tr>`).join("")}
    </table></div>`;
  $("save-tpl").onclick = async () => {
    await api("/api/templates", { method: "POST", body: { name: $("t-name").value, channel: $("t-ch").value, subject: $("t-subj").value, body: $("t-body").value } });
    toast("Template saved"); render();
  };
}

async function renderWorkflows(root) {
  const data = await api("/api/workflows");
  root.innerHTML = `<h1>Workflows</h1>
    <p class="sub">Rules fire when an event is recorded. Pair with Sequences when the next step should wait hours or days.</p>
    <div class="card">
      <label>Automation name</label><input id="w-name" />
      <div class="row"><div><label>When</label>
        <select id="w-trig">
          <option>new DM received</option>
          <option>inbound_message</option>
          <option>new lead received</option>
          <option>comment contains keyword</option>
          <option>mention received</option>
        </select></div>
        <div><label>Keyword (optional)</label><input id="w-key" placeholder="hi" /></div></div>
      <div class="row"><div><label>Then</label>
        <select id="w-act">
          <option>enroll in sequence</option>
          <option>send WhatsApp message</option>
          <option>send email</option>
          <option>send SMS</option>
          <option>create lead</option>
        </select></div>
        <div><label>Message / sequence name</label><input id="w-val" /></div></div>
      <p><button class="btn" id="w-save">Save workflow</button></p>
    </div>
    <div class="card"><h3>Rules</h3>
      <table>${data.rules.map((r) => `<tr><td>${escapeHtml(r.name)}</td><td>${escapeHtml(r.trigger_type)}</td><td>${escapeHtml(r.action_type)}</td><td>${escapeHtml(r.action_value)}</td><td>${r.enabled ? "on" : "off"}</td></tr>`).join("")}</table>
    </div>
    <div class="card"><h3>Recent runs</h3>
      <table>${(data.runs || []).map((r) => `<tr><td>${escapeHtml(r.created_at)}</td><td>${escapeHtml(r.action_type)}</td><td>${escapeHtml(r.status)}</td><td>${escapeHtml(r.detail)}</td></tr>`).join("")}</table>
    </div>`;
  $("w-save").onclick = async () => {
    await api("/api/workflows", { method: "POST", body: {
      name: $("w-name").value, trigger_type: $("w-trig").value, keyword: $("w-key").value,
      action_type: $("w-act").value, action_value: $("w-val").value, enabled: 1,
    }});
    toast("Workflow saved"); render();
  };
}

function stepFields(n, step = {}, accounts = []) {
  return `
    <div class="step">
      <h3>Step ${n}</h3>
      <div class="row3">
        <div><label>Channel</label>
          <select name="ch${n}">
            <option value="whatsapp" ${step.channel === "whatsapp" ? "selected" : ""}>WhatsApp</option>
            <option value="sms" ${step.channel === "sms" ? "selected" : ""}>SMS</option>
            <option value="email" ${step.channel === "email" ? "selected" : ""}>Email</option>
          </select>
        </div>
        <label>Send from account</label><select name="account${n}"><option value="">Workspace default</option>${accounts.filter((account) => account.platform === step.channel).map((account) => `<option value="${escapeHtml(account.id)}" ${account.id === step.account_id ? "selected" : ""}>${escapeHtml(account.name)} · ${escapeHtml(account.identifier)}</option>`).join("")}</select>
        <div><label>Wait hours before this step</label><input name="delay${n}" type="number" min="0" step="0.5" value="${step.delay_hours ?? (n === 1 ? 0 : 24)}" /></div>
        <div><label>Who writes it</label>
          <select name="mode${n}">
            <option value="mine" ${step.mode !== "ai" ? "selected" : ""}>I write it</option>
            <option value="ai" ${step.mode === "ai" ? "selected" : ""}>AI writes it from company docs</option>
          </select>
        </div>
      </div>
      <label>Email subject</label><input name="subj${n}" value="${escapeHtml(step.subject || "")}" />
      <label>Message or AI instruction. Use {{name}}</label>
      <textarea name="body${n}">${escapeHtml(step.body || "")}</textarea>
    </div>`;
}

async function renderSequences(root) {
  const [books, accounts] = await Promise.all([api("/api/sequences"), api("/api/accounts")]);
  const activity = await api("/api/sequences/activity");
  const current = books[0] || { trigger_whatsapp: 1, trigger_new_lead: 1, stop_on_reply: 1, steps: [{}, {}, {}] };
  while (current.steps.length < 3) current.steps.push({});
  root.innerHTML = `<h1>Sequences</h1>
    <p class="sub">Warmup path from the Streamlit Sequences tab: message, wait, next channel. Stops on reply or STOP.</p>
    <p><button class="btn" id="run-seq">Run due steps now</button></p>
    <div class="card">
      <label>Sequence name</label><input id="pb-name" value="${escapeHtml(current.name || "New lead warmup")}" />
      <label>Internal note</label><textarea id="pb-desc">${escapeHtml(current.description || "")}</textarea>
      <p class="sub">Start this sequence when</p>
      <div class="checks">
        <label><input type="checkbox" id="t-wa" ${current.trigger_whatsapp ? "checked" : ""} /> WhatsApp message</label>
        <label><input type="checkbox" id="t-sms" ${current.trigger_sms ? "checked" : ""} /> SMS</label>
        <label><input type="checkbox" id="t-em" ${current.trigger_email ? "checked" : ""} /> Email</label>
        <label><input type="checkbox" id="t-lead" ${current.trigger_new_lead ? "checked" : ""} /> New lead added</label>
        <label><input type="checkbox" id="t-stop" ${current.stop_on_reply ? "checked" : ""} /> Stop if they reply</label>
      </div>
      <form id="pb-form">
        ${stepFields(1, current.steps[0], accounts)}
        ${stepFields(2, current.steps[1], accounts)}
        ${stepFields(3, current.steps[2], accounts)}
        <button class="btn" type="submit">Save sequence</button>
      </form>
    </div>
    <div class="card">
      <h3>Enroll leads</h3>
      <p class="sub">Saved sequences: ${(books || []).map((b) => escapeHtml(b.name)).join(", ") || "none yet"}</p>
    </div>
    <div class="card"><h3>Activity</h3>
      <table>${activity.map((r) => `<tr><td>${escapeHtml(r.sequence_name)}</td><td>${escapeHtml(r.lead_name)}</td><td>${escapeHtml(r.status)}</td><td>${escapeHtml(r.next_run_at)}</td></tr>`).join("")}</table>
    </div>`;
  for (const n of [1, 2, 3]) {
    const channelSelect = root.querySelector(`[name="ch${n}"]`);
    const accountSelect = root.querySelector(`[name="account${n}"]`);
    channelSelect.onchange = () => {
      const matching = accounts.filter((account) => account.platform === channelSelect.value);
      accountSelect.innerHTML = `<option value="">Workspace default</option>${matching.map((account) => `<option value="${escapeHtml(account.id)}">${escapeHtml(account.name)} · ${escapeHtml(account.identifier)}</option>`).join("")}`;
    };
  }
  $("run-seq").onclick = async () => toast(await api("/api/sequences/run", { method: "POST", body: {} }));
  $("pb-form").onsubmit = async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const steps = [1, 2, 3].map((n) => ({
      channel: fd.get("ch" + n),
      account_id: fd.get("account" + n),
      delay_hours: fd.get("delay" + n),
      mode: fd.get("mode" + n),
      subject: fd.get("subj" + n),
      body: fd.get("body" + n),
    }));
    await api("/api/sequences", {
      method: "POST",
      body: {
        id: current.id,
        name: $("pb-name").value,
        description: $("pb-desc").value,
        trigger_whatsapp: $("t-wa").checked,
        trigger_sms: $("t-sms").checked,
        trigger_email: $("t-em").checked,
        trigger_new_lead: $("t-lead").checked,
        stop_on_reply: $("t-stop").checked,
        steps,
      },
    });
    toast("Sequence saved");
    render();
  };
}

async function renderCampaigns(root) {
  const [campaigns, audiences, accounts] = await Promise.all([
    api("/api/campaigns"), api("/api/audiences"), api("/api/accounts"),
  ]);
  const channels = ["email", "whatsapp", "sms", "instagram", "facebook", "linkedin", "x", "tiktok", "youtube"];
  const communication = new Set(["email", "whatsapp", "sms"]);
  root.innerHTML = `<h1>Campaigns</h1>
    <p class="sub">Coordinate audiences, channels, connected accounts, content, and timing in one workspace.</p>
    <div class="card">
      <h2>Create campaign</h2>
      <div class="row"><div><label>Campaign name</label><input id="campaign-name" /></div>
      <div><label>Goal</label><select id="campaign-goal"><option value="follow_up">Lead follow-up</option><option value="announcement">Announcement</option><option value="promotion">Promotion</option><option value="product_launch">Product launch</option></select></div></div>
      <h3>Audience</h3>
      <div class="row"><div><label>Audience type</label><select id="audience-mode"><option value="all">All leads</option><option value="custom">Custom filters</option><option value="saved">Saved audience</option></select></div>
      <div id="saved-audience-wrap" class="hide"><label>Saved audience</label><select id="saved-audience">${audiences.map((audience) => `<option value="${escapeHtml(audience.id)}">${escapeHtml(audience.name)} (${audience.lead_count})</option>`).join("")}</select></div></div>
      <div id="audience-filters" class="row3">
        <div><label>Stage</label><select id="audience-stage"><option value="">Any</option>${["new", "contacted", "replied", "booked", "won", "lost"].map((stage) => `<option>${stage}</option>`).join("")}</select></div>
        <div><label>Source</label><input id="audience-source" placeholder="Any source" /></div>
        <div><label>Tag</label><input id="audience-tag" placeholder="Any tag" /></div>
        <div><label>Has email</label><select id="audience-email"><option value="">Any</option><option value="yes">Yes</option><option value="no">No</option></select></div>
        <div><label>Has phone</label><select id="audience-phone"><option value="">Any</option><option value="yes">Yes</option><option value="no">No</option></select></div>
        <div><label>Has WhatsApp number</label><select id="audience-whatsapp"><option value="">Any</option><option value="yes">Yes</option><option value="no">No</option></select></div>
        <div><label>Created after</label><input id="audience-after" type="date" /></div>
        <div><label>Created before</label><input id="audience-before" type="date" /></div>
        <div><label>Last contacted after</label><input id="audience-contact-after" type="date" /></div>
        <div><label>Last contacted before</label><input id="audience-contact-before" type="date" /></div>
      </div>
      <div class="row"><p><button class="btn ghost" id="audience-preview">Preview audience</button> <button class="btn ghost" id="audience-save">Save audience</button></p><p class="sub" id="audience-count" aria-live="polite"></p></div>
      <h3>Channels</h3>
      <div class="checks">${channels.map((channel) => `<label><input type="checkbox" name="campaign-channel" value="${channel}" /> ${channel[0].toUpperCase() + channel.slice(1)}</label>`).join("")}</div>
      <h3>Accounts</h3>
      <div class="checks" id="campaign-accounts">${accounts.map((account) => `<label><input type="checkbox" name="campaign-account" value="${escapeHtml(account.id)}" /> ${escapeHtml(account.name)} · ${escapeHtml(account.platform)}</label>`).join("") || "No connected accounts."}</div>
      <div class="row"><div><label>Schedule</label><input id="campaign-schedule" type="datetime-local" /></div>
      <div><label>Posting strategy</label><select id="campaign-strategy"><option value="same">Same content for selected accounts</option><option value="customized">Customized per account</option><option value="separate">Schedule separately</option></select></div></div>
      <label>Campaign content</label><textarea id="campaign-content" placeholder="Message or post copy"></textarea>
      <p><button class="btn" id="campaign-create">Save campaign</button></p>
    </div>
    <div class="card"><h2>Saved audiences</h2><table><thead><tr><th>Name</th><th>Leads</th><th>Created</th></tr></thead><tbody>${audiences.map((audience) => `<tr><td>${escapeHtml(audience.name)}</td><td>${audience.lead_count}</td><td>${escapeHtml(audience.created_at)}</td></tr>`).join("")}</tbody></table></div>
    <div class="card"><h2>Campaigns</h2><table><thead><tr><th>Campaign</th><th>Status</th><th>Audience</th><th>Channels</th><th>Schedule</th><th></th></tr></thead><tbody>${campaigns.map((campaign) => `<tr><td>${escapeHtml(campaign.name)}</td><td>${escapeHtml(campaign.status)}</td><td>${campaign.lead_count}</td><td>${escapeHtml(campaign.channels.join(", "))}</td><td>${escapeHtml(campaign.scheduled_at || "Not scheduled")}</td><td><select data-campaign-status="${escapeHtml(campaign.id)}"><option ${campaign.status === "draft" ? "selected" : ""}>draft</option><option ${campaign.status === "scheduled" ? "selected" : ""}>scheduled</option><option ${campaign.status === "paused" ? "selected" : ""}>paused</option><option ${campaign.status === "completed" ? "selected" : ""}>completed</option></select></td></tr>`).join("")}</tbody></table></div>`;

  const audienceMode = $("audience-mode");
  const filtersWrap = $("audience-filters");
  const savedWrap = $("saved-audience-wrap");
  const readFilters = () => ({
    stage: $("audience-stage").value,
    source: $("audience-source").value,
    tag: $("audience-tag").value,
    has_email: $("audience-email").value === "" ? null : $("audience-email").value === "yes",
    has_phone: $("audience-phone").value === "" ? null : $("audience-phone").value === "yes",
    has_whatsapp: $("audience-whatsapp").value === "" ? null : $("audience-whatsapp").value === "yes",
    created_after: $("audience-after").value,
    created_before: $("audience-before").value,
    last_contacted_after: $("audience-contact-after").value,
    last_contacted_before: $("audience-contact-before").value,
  });
  const selectedAudience = () => audiences.find((audience) => audience.id === $("saved-audience").value);
  const selectedFilters = () => audienceMode.value === "saved" ? (selectedAudience()?.filters || {}) : audienceMode.value === "all" ? {} : readFilters();
  const updateAudienceMode = () => {
    filtersWrap.classList.toggle("hide", audienceMode.value !== "custom");
    savedWrap.classList.toggle("hide", audienceMode.value !== "saved");
  };
  audienceMode.onchange = updateAudienceMode;
  updateAudienceMode();
  $("audience-preview").onclick = async () => {
    const result = await api("/api/audiences/preview", { method: "POST", body: { filters: selectedFilters() } });
    $("audience-count").textContent = `${result.count.toLocaleString()} leads match`;
  };
  $("audience-save").onclick = async () => {
    const name = window.prompt("Name this audience");
    if (!name?.trim()) return;
    const result = await api("/api/audiences", { method: "POST", body: { name: name.trim(), filters: readFilters() } });
    toast(`Saved audience with ${result.lead_count} leads`);
    render();
  };
  $("campaign-create").onclick = async () => {
    const selectedChannels = [...root.querySelectorAll("input[name=campaign-channel]:checked")].map((input) => input.value);
    const selectedAccounts = [...root.querySelectorAll("input[name=campaign-account]:checked")].map((input) => input.value);
    const filters = selectedFilters();
    const savedId = audienceMode.value === "saved" ? $("saved-audience").value : "";
    const scheduledAt = $("campaign-schedule").value;
    const result = await api("/api/campaigns", { method: "POST", body: {
      name: $("campaign-name").value,
      goal: $("campaign-goal").value,
      audience_id: savedId,
      audience_filters: filters,
      channels: selectedChannels,
      account_ids: selectedAccounts,
      content: { body: $("campaign-content").value, posting_strategy: $("campaign-strategy").value },
      scheduled_at: scheduledAt,
      status: scheduledAt ? "scheduled" : "draft",
      limits: {}, tracking: {},
    }});
    toast(`Campaign saved for ${result.lead_count} leads`);
    render();
  };
  root.querySelectorAll("select[data-campaign-status]").forEach((select) => {
    select.onchange = async () => {
      await api(`/api/campaigns/${select.dataset.campaignStatus}/status`, { method: "POST", body: { status: select.value } });
      toast("Campaign status updated");
    };
  });
}

