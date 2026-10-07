async function renderSocial(root) {
  const [accounts, rows, automations, events, templates] = await Promise.all([
    api("/api/accounts"), api("/api/schedule"), api("/api/social/comment-automations"),
    api("/api/social/comment-automations/events"), api("/api/templates"),
  ]);
  const platforms = await api("/api/social/platforms");
  const socialAccounts = accounts.filter((account) => account.account_type === "social");
  const connectedSocialAccounts = socialAccounts.filter((account) => account.status === "connected");
  const publishAccounts = connectedSocialAccounts.filter((account) => ["instagram", "threads", "linkedin"].includes(account.platform));
  const replyAccounts = connectedSocialAccounts.filter((account) => ["instagram", "facebook", "threads"].includes(account.platform));
  const templateChannel = { instagram: "instagram_dm", facebook: "messenger", threads: "threads_reply" };
  const replyTemplates = templates.filter((template) => Object.values(templateChannel).includes(template.channel));
  root.innerHTML = `<h1>Social accounts</h1>
    <p class="sub">Connect accounts, queue supported posts, and create comment-triggered replies using a template you choose.</p>
    <div class="card">
      <div class="row3">
        <div><label>Platform</label><select id="s-plat">${platforms.map((p) => `<option>${p}</option>`).join("")}</select></div>
        <div><label>Platform account ID / author URN</label><input id="s-handle" placeholder="Instagram: professional account ID; Threads: user ID; LinkedIn: author URN" /></div>
        <div><label>Access token</label><input id="s-tok" type="password" /></div>
      </div>
      <p><button class="btn" id="s-save">Connect account</button></p>
      <table>${socialAccounts.map((a) => `<tr><td>${escapeHtml(a.platform)}</td><td>${escapeHtml(a.identifier)}</td><td>${escapeHtml(a.status)}</td></tr>`).join("")}</table>
    </div>
    <div class="card">
      <h3>Schedule</h3>
      <div class="row"><div><label>LinkedIn post idea</label><input id="linkedin-brief" placeholder="What should the post teach, share, or announce?" /></div><div><label>&nbsp;</label><button class="btn ghost" id="linkedin-draft">Draft for review</button></div></div>
      <label>Caption</label><textarea id="cap"></textarea>
      <label>Public video URL for an Instagram Reel (optional)</label><input id="post-media" type="url" placeholder="https://.../video.mp4" />
      <p class="sub">Instagram Reel publishing needs a public HTTPS video URL. Threads scheduling currently supports text posts.</p>
      <label>Publish through supported accounts</label><div class="checks" id="post-accounts">${publishAccounts.map((account) => `<label><input type="checkbox" name="post-account" value="${escapeHtml(account.id)}" /> ${escapeHtml(account.name)} · ${escapeHtml(account.platform)}</label>`).join("") || "Connect LinkedIn, Instagram, or Threads to schedule supported posts."}</div>
      <label>When UTC</label><input id="when" placeholder="2026-10-01T10:00:00Z" />
      <p><button class="btn" id="qpost">Queue post</button></p>
      <table>${rows.map((r) => `<tr><td>${escapeHtml(r.scheduled_at)}</td><td>${escapeHtml(r.status)}</td><td>${escapeHtml(r.caption)}</td><td>${escapeHtml(r.detail || "")}</td></tr>`).join("")}</table>
    </div>`;
  root.insertAdjacentHTML("beforeend", `<div class="card">
    <h3>Comment automations</h3>
    <p class="sub">Instagram and Facebook Page comments can trigger one private reply using the matching template. Threads replies use a public reply because the official Threads API does not provide a DM send endpoint. Meta comment webhooks must be subscribed to <code>comments</code> (Instagram), <code>feed</code> (Facebook Page), or <code>replies</code> (Threads).</p>
    <div class="row3">
      <div><label>Automation name</label><input id="ca-name" placeholder="Send the reel guide" /></div>
      <div><label>Account</label><select id="ca-account">${replyAccounts.map((account) => `<option value="${escapeHtml(account.id)}" data-platform="${escapeHtml(account.platform)}">${escapeHtml(account.name)} · ${escapeHtml(account.platform)}</option>`).join("") || `<option value="">Connect an account first</option>`}</select></div>
      <div><label>Reply template</label><select id="ca-template"></select></div>
    </div>
    <div class="row3"><div><label>Root post / Reel ID (optional)</label><input id="ca-post" placeholder="Leave blank to match any post on this account" /></div><div><label>Comment keyword (optional)</label><input id="ca-keyword" placeholder="guide" /></div><div><label>&nbsp;</label><button class="btn" id="ca-save" ${!replyAccounts.length ? "disabled" : ""}>Save automation</button></div></div>
    <table><thead><tr><th>Rule</th><th>Account</th><th>Post</th><th>Keyword</th><th>Template</th><th>Status</th><th></th></tr></thead><tbody>${automations.map((rule) => `<tr><td>${escapeHtml(rule.name)}</td><td>${escapeHtml(rule.platform)} · ${escapeHtml(rule.account_handle)}</td><td>${escapeHtml(rule.post_id || "All posts")}</td><td>${escapeHtml(rule.keyword || "Any comment")}</td><td>${escapeHtml(rule.template_name)}</td><td>${rule.enabled ? "Enabled" : "Paused"} · ${Number(rule.executions || 0)} sent</td><td><button class="btn ghost" data-ca-toggle="${escapeHtml(rule.id)}" data-enabled="${rule.enabled ? "0" : "1"}">${rule.enabled ? "Pause" : "Resume"}</button></td></tr>`).join("") || `<tr><td colspan="7">No comment automations configured.</td></tr>`}</tbody></table>
    <h3>Recent comment replies</h3><table><thead><tr><th>When</th><th>Platform</th><th>Comment</th><th>Response</th><th>Status</th><th>Details</th></tr></thead><tbody>${events.map((event) => `<tr><td>${escapeHtml(event.created_at)}</td><td>${escapeHtml(event.platform)}</td><td>${escapeHtml(event.comment_text || "").slice(0, 100)}</td><td>${escapeHtml(event.response_id || "")}</td><td>${escapeHtml(event.status)}</td><td>${escapeHtml(event.detail || "")}</td></tr>`).join("") || `<tr><td colspan="6">No comment events received yet.</td></tr>`}</tbody></table>
  </div>`);
  const updateAutomationTemplates = () => {
    const platform = $("ca-account")?.selectedOptions?.[0]?.dataset?.platform;
    const channel = templateChannel[platform];
    const options = replyTemplates.filter((template) => template.channel === channel);
    $("ca-template").innerHTML = options.map((template) => `<option value="${escapeHtml(template.id)}">${escapeHtml(template.name)}</option>`).join("") || `<option value="">Create a ${escapeHtml(channel || "social reply")} template first</option>`;
  };
  $("ca-account")?.addEventListener("change", updateAutomationTemplates);
  updateAutomationTemplates();
  $("s-save").onclick = async () => {
    await api("/api/social/accounts", { method: "POST", body: { platform: $("s-plat").value, handle: $("s-handle").value, access_token: $("s-tok").value } });
    toast("Account saved"); render();
  };
  $("qpost").onclick = async () => {
    const accountIds = [...root.querySelectorAll("input[name=post-account]:checked")].map((input) => input.value);
    await api("/api/schedule", { method: "POST", body: { caption: $("cap").value, scheduled_at: $("when").value, account_ids: accountIds, media: $("post-media").value } });
    toast("Queued"); render();
  };
  $("linkedin-draft").onclick = async () => {
    const result = await api("/api/social/draft", { method: "POST", body: { platform: "linkedin", brief: $("linkedin-brief").value } });
    $("cap").value = result.draft;
    toast("Draft ready. Review it, select a LinkedIn account, then queue it.");
  };
  $("ca-save")?.addEventListener("click", async () => {
    await api("/api/social/comment-automations", { method: "POST", body: {
      name: $("ca-name").value,
      account_id: $("ca-account").value,
      template_id: $("ca-template").value,
      post_id: $("ca-post").value,
      keyword: $("ca-keyword").value,
    } });
    toast("Comment automation saved"); render();
  });
  root.querySelectorAll("button[data-ca-toggle]").forEach((button) => {
    button.onclick = async () => {
      await api(`/api/social/comment-automations/${encodeURIComponent(button.dataset.caToggle)}/enabled`, { method: "POST", body: { enabled: button.dataset.enabled === "1" } });
      render();
    };
  });
}

async function renderAds(root, selectedAccountId = "") {
  const [providers, optimizationRules] = await Promise.all([api("/api/ads/providers"), api("/api/ads/optimization-rules")]);
  const adAccounts = providers.flatMap((provider) => (provider.accounts || []).map((account) => ({ ...account, platform: provider.id, provider: provider.name })));
  const selected = adAccounts.find((account) => account.id === selectedAccountId) || adAccounts[0] || null;
  const selectedProvider = providers.find((provider) => provider.id === selected?.platform);
  let campaigns = [];
  let error = "";
  if (selected) {
    try {
      const live = await api(`/api/ads/platform-accounts/${encodeURIComponent(selected.id)}/campaigns`);
      campaigns = live.campaigns || [];
      selected.status = "validated";
      if (selectedProvider) {
        selectedProvider.status = "validated";
        selectedProvider.accounts = (selectedProvider.accounts || []).map((account) => account.id === selected.id ? { ...account, status: "validated" } : account);
      }
    } catch (err) {
      error = err.message;
    }
  }
  const campaignValue = (campaign, keys) => {
    for (const key of keys) if (campaign[key] !== undefined && campaign[key] !== "") return campaign[key];
    return "";
  };
  const hasPerformanceMetrics = campaigns.some((campaign) => campaign.impressions !== undefined || campaign.clicks !== undefined || campaign.spend_amount !== undefined || campaign.spend_micros !== undefined);
  const hasBudget = campaigns.some((campaign) => campaign.budget_amount !== undefined && campaign.budget_amount !== "");
  const hasConversions = campaigns.some((campaign) => campaign.conversions !== undefined);
  const inputField = (key, label, placeholder = "", type = "text") => `<div><label for="ad-extra-${key}">${label}</label><input id="ad-extra-${key}" data-ad-extra="${key}" type="${type}" placeholder="${placeholder}" /></div>`;
  const choiceField = (key, label, choices) => `<div><label for="ad-extra-${key}">${label}</label><select id="ad-extra-${key}" data-ad-extra="${key}">${choices.map(([value, title]) => `<option value="${value}">${title}</option>`).join("")}</select></div>`;
  const textField = (key, label, placeholder = "") => `<div><label for="ad-extra-${key}">${label}</label><textarea id="ad-extra-${key}" data-ad-extra="${key}" placeholder="${placeholder}"></textarea></div>`;
  const builderFields = selected?.platform === "meta_ads" ? `<div class="row3">${inputField("page_id", "Facebook Page ID")}${inputField("image_url", "Public HTTPS image URL", "https://example.com/ad.jpg", "url")}${inputField("country_code", "Country code", "US")}</div><div class="row3">${choiceField("objective", "Objective", [["OUTCOME_TRAFFIC", "Traffic (link clicks)"]])}${inputField("headline", "Headline")}${textField("body", "Ad text")}</div>`
    : selected?.platform === "linkedin_ads" ? `<div class="row3">${inputField("campaign_group_urn", "Campaign group URN")}${inputField("currency_code", "Currency code", "USD")}${choiceField("objective", "Objective", [["WEBSITE_VISITS", "Website visits"]])}</div><div class="row3">${inputField("country_code", "Country code", "US")}${inputField("language_code", "Language code", "en")}${inputField("headline", "Text ad title")}</div><div class="row3">${textField("body", "Text ad copy")}${textField("targeting_criteria", "Required LinkedIn targeting JSON", '{"include":{"and":[{"or":{"urn:li:adTargetingFacet:locations":["urn:li:geo:103644278"]}}]}}')}</div>`
    : selected?.platform === "x_ads" ? `<div class="row3">${inputField("funding_instrument_id", "Funding instrument ID")}${inputField("tweet_id", "Existing approved Post ID to promote")}${inputField("bid", "Line item bid", "1.00", "number")}</div>`
    : selected?.platform === "tiktok_ads" ? `<div class="row3">${inputField("video_id", "Uploaded video ID")}${inputField("identity_id", "Authorized identity ID")}${inputField("location_id", "TikTok location ID")}</div><div class="row3">${inputField("identity_type", "Identity type", "BC_AUTH_TT")}${choiceField("objective", "Objective", [["TRAFFIC", "Traffic (clicks)"]])}${inputField("call_to_action", "Call to action", "LEARN_MORE")}</div>${textField("body", "Ad text")}`
    : selected?.platform === "microsoft_ads" ? `<div class="row3">${inputField("time_zone", "Microsoft API time zone", "PacificTimeUSCanadaTijuana")}${inputField("location_id", "Numeric location ID")}${inputField("language", "Campaign language", "English")}</div><div class="row3">${inputField("max_cpc", "Max CPC", "1.00", "number")}${inputField("match_type", "Keyword match type", "Phrase")}${textField("keywords", "Keywords (one per line)")}</div><div class="row3">${textField("headlines", "RSA headlines (3–15; one per line)")}${textField("descriptions", "RSA descriptions (2–4; one per line)")}</div>`
    : selected?.platform === "pinterest_ads" ? `<div class="row3">${inputField("pin_id", "Existing ad-only Pin ID")}${inputField("country_code", "Country code", "US")}${choiceField("objective", "Objective", [["CONSIDERATION", "Consideration"]])}</div>${inputField("bid", "Ad group bid", "1.00", "number")}`
    : selected?.platform === "snapchat_ads" ? `<div class="row3">${inputField("media_id", "Uploaded Top Snap media ID")}${inputField("profile_id", "Public Profile ID")}${inputField("country_code", "Country code", "us")}</div><div class="row3">${inputField("headline", "Ad headline")}${choiceField("objective", "Objective", [["AWARENESS_AND_ENGAGEMENT", "Awareness and engagement"]])}${inputField("bid", "Ad squad bid", "1.00", "number")}</div>` : "";
  root.innerHTML = `<h1>Advertising</h1>
    <p class="sub">Connect each advertiser account, then sync campaigns from the provider API. YouTube ad placements are managed through Google Ads.</p>
    <div class="card"><div class="row"><div><label>Advertiser account</label><select id="ad-select">${adAccounts.map((account) => `<option value="${escapeHtml(account.id)}" ${account.id === selected?.id ? "selected" : ""}>${escapeHtml(account.provider)} · ${escapeHtml(account.name)} · ${escapeHtml(account.identifier)}</option>`).join("") || `<option value="">No ad accounts connected</option>`}</select></div>
      <div><label>Connection</label><p class="sub">${selected ? `${escapeHtml(selected.provider)} · ${escapeHtml(selected.status)}` : "Add an ad account under Connected Accounts."}</p></div></div>
      ${selected ? `<p><button class="btn" id="ad-sync">Sync campaigns</button></p>` : `<p class="sub">Connect a provider under Connected Accounts to load its campaigns.</p>`}
      ${error ? `<p class="sub">${escapeHtml(error)}</p>` : ""}
      <p class="sub">Activating a campaign can spend the advertiser’s budget. Each activation below requires a confirmation. Legacy Google Video campaigns are report-only in the Google Ads API.</p>
      <p class="sub">${escapeHtml(selectedProvider?.reporting || "Campaign status only.")}</p>
      <table><thead><tr><th>Campaign</th><th>Status</th><th>Objective</th>${hasBudget ? "<th>Daily budget</th>" : ""}${hasPerformanceMetrics ? "<th>Impressions</th><th>Clicks / swipes</th><th>Spend</th>" : ""}${hasConversions ? "<th>Conversions</th>" : ""}<th></th></tr></thead><tbody>${campaigns.map((campaign) => {
        const campaignId = campaignValue(campaign, ["id", "campaignId", "campaign_id"]);
        const campaignStatus = campaignValue(campaign, ["status", "effective_status", "operation_status"]);
        const active = ["ACTIVE", "ENABLED", "RUNNING"].includes(String(campaignStatus).toUpperCase());
        const cost = campaign.spend_amount !== undefined ? Number(campaign.spend_amount) : Number(campaign.spend_micros || 0) / 1000000;
        const formattedCost = Number.isFinite(cost) ? `${cost.toFixed(2)} ${campaign.currency || ""}`.trim() : "";
        const budget = Number(campaign.budget_amount || 0);
        const rule = optimizationRules.find((item) => item.account_id === selected?.id && String(item.campaign_id) === String(campaignId));
        const readOnlyGoogleVideo = selected?.platform === "google_ads" && campaign.objective === "VIDEO";
        return `<tr><td>${escapeHtml(campaignValue(campaign, ["name", "campaignName", "campaign_name"]))}</td><td>${escapeHtml(campaignStatus)}</td><td>${escapeHtml(campaignValue(campaign, ["objective", "objectiveType", "objective_type", "advertisingChannelType", "campaignType"]))}</td>${hasBudget ? `<td>${escapeHtml(`${budget.toFixed(2)} ${campaign.currency || ""}`.trim())}${campaign.budget_shared ? " · shared" : ""}</td>` : ""}${hasPerformanceMetrics ? `<td>${escapeHtml(campaignValue(campaign, ["impressions"]))}</td><td>${escapeHtml(campaignValue(campaign, ["clicks"]))}</td><td>${escapeHtml(formattedCost)}</td>` : ""}${hasConversions ? `<td>${escapeHtml(campaignValue(campaign, ["conversions"]))}</td>` : ""}<td>${readOnlyGoogleVideo ? "Report only · manage in Google Ads" : `<button class="btn ghost" data-ad-status="${escapeHtml(campaignId)}" data-status="${active ? "PAUSED" : "ACTIVE"}" ${!campaignId ? "disabled" : ""}>${active ? "Pause" : "Activate"}</button> <button class="btn ghost" data-ad-target="${escapeHtml(campaignId)}">Edit targeting</button>${!campaign.budget_shared ? ` <button class="btn ghost" data-ad-budget="${escapeHtml(campaignId)}" data-budget="${Number.isFinite(budget) ? budget : ""}">Edit budget</button>` : ""}${selected ? ` <button class="btn ghost" data-ad-opt="${escapeHtml(campaignId)}" data-spend="${escapeHtml(rule?.minimum_30d_spend ?? "")}" data-conversions="${escapeHtml(rule?.maximum_30d_conversions ?? "")}" data-conversion-enabled="${rule?.conversion_limit_enabled ? "1" : "0"}">${rule?.enabled ? "Edit guardrail" : "Auto-pause rule"}</button>` : ""}`}</td></tr>`;
      }).join("") || `<tr><td colspan="${3 + (hasBudget ? 1 : 0) + (hasPerformanceMetrics ? 3 : 0) + (hasConversions ? 1 : 0) + 1}">${selected ? "No campaigns returned. Sync after checking provider access and account IDs." : "No connected ad account."}</td></tr>`}</tbody></table>
    </div>
    ${selected?.platform === "google_ads" ? `<div class="card"><h3>Create a paused Google Search ad</h3><p class="sub">Creates the campaign, dedicated daily budget, location and language targeting, ad group, keywords, and responsive search ad. The campaign stays paused until you activate it after review.</p>
      <div class="row3"><div><label>Campaign name</label><input id="new-ad-name" maxlength="128" /></div><div><label>Daily budget (${escapeHtml(campaigns[0]?.currency || "account currency")})</label><input id="new-ad-budget" type="number" min="0.01" step="0.01" /></div><div><label>Max CPC bid (${escapeHtml(campaigns[0]?.currency || "account currency")})</label><input id="new-ad-cpc" type="number" min="0.01" step="0.01" /></div></div>
      <div class="row3"><div><label>Final URL</label><input id="new-ad-url" type="url" placeholder="https://example.com/product" /></div><div><label>Google geo target constant ID</label><input id="new-ad-geo" inputmode="numeric" placeholder="Required; choose the location to target" /></div><div><label>Language constant ID</label><input id="new-ad-language" inputmode="numeric" value="1000" /></div></div>
      <p class="sub">Look up a location ID in <a href="https://developers.google.com/google-ads/api/data/geotargets" target="_blank" rel="noopener noreferrer">Google's geo target reference</a>. English is language constant 1000.</p>
      <div class="row3"><div><label>Keywords (comma or newline separated)</label><textarea id="new-ad-keywords" placeholder="your product&#10;service near me"></textarea><label>Match type</label><select id="new-ad-match"><option>EXACT</option><option>PHRASE</option><option>BROAD</option></select></div><div><label>Ad headlines (3–15, one per line)</label><textarea id="new-ad-headlines" placeholder="Useful headline one&#10;Useful headline two&#10;Useful headline three"></textarea></div><div><label>Ad descriptions (2–4, one per line)</label><textarea id="new-ad-descriptions" placeholder="Clear benefit and next step.&#10;Another specific reason to visit."></textarea></div></div>
      <p><button class="btn" id="ad-create">Create paused Search ad</button></p></div>` : ""}
    ${selected?.platform === "google_ads" ? `<div class="card"><h3>Create a paused YouTube Demand Gen campaign</h3><p class="sub">Demand Gen can use YouTube in-feed, in-stream, and Shorts inventory. Google Ads requires the video and logo asset to exist in the account first. Its API does not allow creating standalone Video campaigns.</p><div class="row3"><div><label>Campaign name</label><input id="youtube-ad-name" maxlength="128" /></div><div><label>Daily budget (${escapeHtml(campaigns[0]?.currency || "account currency")})</label><input id="youtube-ad-budget" type="number" min="0.01" step="0.01" /></div><div><label>Public HTTPS landing page</label><input id="youtube-ad-url" type="url" placeholder="https://example.com/product" /></div></div><div class="row3"><div><label>YouTube video ID</label><input id="youtube-ad-video" /></div><div><label>Existing logo asset resource name</label><input id="youtube-ad-logo" placeholder="customers/123/assets/456" /></div><div><label>Business name</label><input id="youtube-ad-business" /></div></div><div class="row3"><div><label>Google geo target constant ID</label><input id="youtube-ad-geo" inputmode="numeric" /></div><div><label>Language constant ID</label><input id="youtube-ad-language" inputmode="numeric" value="1000" /></div><div><label>Long headline (90 characters max)</label><input id="youtube-ad-long-headline" maxlength="90" /></div></div><div class="row3"><div><label>Short headlines (3–5; one per line)</label><textarea id="youtube-ad-headlines"></textarea></div><div><label>Descriptions (one per line)</label><textarea id="youtube-ad-descriptions"></textarea></div></div><p><button class="btn" id="ad-create-youtube">Create paused YouTube Demand Gen campaign</button></p></div>` : ""}
    ${selected && selected.platform !== "google_ads" ? `<div class="card"><h3>Create a paused ${escapeHtml(selectedProvider?.name || "advertising")} campaign</h3><p class="sub">Creates the provider campaign structure and ad using the required fields or existing provider assets. It stays paused for review before activation.</p><div class="row3"><div><label>Campaign name</label><input id="new-provider-name" maxlength="128" /></div><div><label>Daily budget (account currency)</label><input id="new-provider-budget" type="number" min="0.01" step="0.01" /></div><div><label>Final URL, where required</label><input id="new-provider-url" type="url" placeholder="https://example.com/product" /></div></div>${builderFields}<p><button class="btn" id="ad-create-provider">Create paused campaign</button></p></div>` : ""}
    ${selected ? `<div class="card"><h3>Automatic pause guardrails</h3><p class="sub">A daily worker applies the same per-campaign spend and optional conversion rules across connected ad networks. It pauses campaigns when reported last-30-day spend reaches the limit and conversions are at or below the chosen count. It never reactivates campaigns or shifts budgets between networks.</p><table><thead><tr><th>Campaign</th><th>Pause at spend</th><th>When conversions ≤</th><th>Last check</th><th>Last action</th><th>Details</th><th></th></tr></thead><tbody>${optimizationRules.filter((rule) => rule.account_id === selected?.id).map((rule) => {
      const campaign = campaigns.find((item) => String(campaignValue(item, ["id", "campaignId", "campaign_id"])) === String(rule.campaign_id));
      return `<tr><td>${escapeHtml(campaignValue(campaign || {}, ["name", "campaignName", "campaign_name"]) || rule.campaign_id)}</td><td>${escapeHtml(rule.minimum_30d_spend)}</td><td>${escapeHtml(rule.maximum_30d_conversions)}</td><td>${escapeHtml(rule.last_checked_at || "Waiting for worker")}</td><td>${escapeHtml(rule.enabled ? (rule.last_action || "enabled") : "paused")}</td><td>${escapeHtml(rule.last_detail || "")}</td><td><button class="btn ghost" data-opt-toggle="${escapeHtml(rule.id)}" data-enabled="${rule.enabled ? "0" : "1"}">${rule.enabled ? "Pause rule" : "Resume rule"}</button></td></tr>`;
    }).join("") || `<tr><td colspan="7">No guardrails configured for this account.</td></tr>`}</tbody></table></div>` : ""}
    <div class="card"><h3>Ad platforms</h3><table><thead><tr><th>Platform</th><th>Placements</th><th>Accounts</th><th>Connection</th><th>Available automation</th><th>Reporting</th><th>Setup</th></tr></thead><tbody>${providers.map((provider) => `<tr><td><a href="${escapeHtml(provider.docs)}" target="_blank" rel="noopener noreferrer">${escapeHtml(provider.name)}</a></td><td>${escapeHtml(provider.placements)}</td><td>${(provider.accounts || []).length}</td><td>${escapeHtml(provider.status)}</td><td>${escapeHtml(provider.automation)}</td><td>${escapeHtml(provider.reporting || "Campaign status only.")}</td><td>${escapeHtml(provider.setup)}${(provider.credential_urls || []).map((item) => ` <a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.label)}</a>`).join(" · ")}</td></tr>`).join("")}</tbody></table></div>`;
  if ($("ad-select")) $("ad-select").onchange = () => renderAds(root, $("ad-select").value);
  if ($("ad-sync")) $("ad-sync").onclick = () => renderAds(root, selected.id);
  if ($("ad-create")) $("ad-create").onclick = async () => {
    const name = $("new-ad-name").value.trim();
    const dailyBudget = $("new-ad-budget").value;
    if (!name || !dailyBudget) { toast("Enter a campaign name and daily budget."); return; }
    try {
      const result = await api(`/api/ads/platform-accounts/${encodeURIComponent(selected.id)}/campaigns`, { method: "POST", body: {
        name, daily_budget: dailyBudget, max_cpc: $("new-ad-cpc").value, final_url: $("new-ad-url").value,
        geo_target_constant_id: $("new-ad-geo").value, language_constant_id: $("new-ad-language").value,
        keywords: $("new-ad-keywords").value, match_type: $("new-ad-match").value,
        headlines: $("new-ad-headlines").value, descriptions: $("new-ad-descriptions").value,
      } });
      toast(`Paused Search ad created${result.campaign_id ? ` · ${result.campaign_id}` : ""}`);
      renderAds(root, selected.id);
    } catch (err) { toast(err.message); }
  };
  if ($("ad-create-provider")) $("ad-create-provider").onclick = async () => {
    const payload = {
      name: $("new-provider-name").value.trim(), daily_budget: $("new-provider-budget").value,
      final_url: $("new-provider-url").value.trim(),
    };
    if (!payload.name || !payload.daily_budget) { toast("Enter a campaign name and daily budget."); return; }
    try {
      for (const input of root.querySelectorAll("[data-ad-extra]")) payload[input.dataset.adExtra] = input.value.trim();
      if (payload.targeting_criteria) {
        try { payload.targeting_criteria = JSON.parse(payload.targeting_criteria); }
        catch { toast("Targeting criteria must be valid JSON."); return; }
      }
      const result = await api(`/api/ads/platform-accounts/${encodeURIComponent(selected.id)}/campaigns`, { method: "POST", body: payload });
      toast(`${result.status === "DISABLED" ? "Disabled" : "Paused"} campaign created${result.campaign_id ? ` · ${result.campaign_id}` : ""}`);
      renderAds(root, selected.id);
    } catch (err) { toast(err.message); }
  };
  if ($("ad-create-youtube")) $("ad-create-youtube").onclick = async () => {
    const payload = {
      campaign_type: "DEMAND_GEN", name: $("youtube-ad-name").value.trim(), daily_budget: $("youtube-ad-budget").value,
      final_url: $("youtube-ad-url").value.trim(), youtube_video_id: $("youtube-ad-video").value.trim(),
      logo_asset_resource_name: $("youtube-ad-logo").value.trim(), business_name: $("youtube-ad-business").value.trim(),
      geo_target_constant_id: $("youtube-ad-geo").value.trim(), language_constant_id: $("youtube-ad-language").value.trim(),
      long_headline: $("youtube-ad-long-headline").value.trim(), headlines: $("youtube-ad-headlines").value,
      descriptions: $("youtube-ad-descriptions").value,
    };
    if (!payload.name || !payload.daily_budget) { toast("Enter a campaign name and daily budget."); return; }
    try {
      const result = await api(`/api/ads/platform-accounts/${encodeURIComponent(selected.id)}/campaigns`, { method: "POST", body: payload });
      toast(`Paused YouTube Demand Gen campaign created${result.campaign_id ? ` · ${result.campaign_id}` : ""}`);
      renderAds(root, selected.id);
    } catch (err) { toast(err.message); }
  };
  root.querySelectorAll("button[data-ad-status]").forEach((button) => {
    button.onclick = async () => {
      const status = button.dataset.status;
      if (status === "ACTIVE" && !window.confirm("This campaign may begin spending the account’s budget. Activate it?")) return;
      try {
        await api(`/api/ads/platform-accounts/${encodeURIComponent(selected.id)}/campaigns/${encodeURIComponent(button.dataset.adStatus)}/status`, { method: "POST", body: { status } });
        toast(status === "ACTIVE" ? "Campaign activated" : "Campaign paused");
        renderAds(root, selected.id);
      } catch (err) { toast(err.message); }
    };
  });
  root.querySelectorAll("button[data-ad-budget]").forEach((button) => {
    button.onclick = async () => {
      const dailyBudget = window.prompt(`New daily budget in ${campaigns[0]?.currency || "the ad account currency"}:`, button.dataset.budget);
      if (dailyBudget === null || !dailyBudget.trim()) return;
      const needsGroup = ["meta_ads", "tiktok_ads"].includes(selected.platform);
      const adGroupId = needsGroup ? window.prompt(`${selected.platform === "meta_ads" ? "Meta ad set" : "TikTok ad group"} ID to update its daily budget:`) : "";
      if (needsGroup && !adGroupId?.trim()) return;
      if (!window.confirm(`Set this campaign's daily budget to ${dailyBudget}?`)) return;
      try {
        await api(`/api/ads/platform-accounts/${encodeURIComponent(selected.id)}/campaigns/${encodeURIComponent(button.dataset.adBudget)}/budget`, { method: "POST", body: { daily_budget: dailyBudget, ad_group_id: adGroupId } });
        toast("Daily budget updated");
        renderAds(root, selected.id);
      } catch (err) { toast(err.message); }
    };
  });
  root.querySelectorAll("button[data-ad-target]").forEach((button) => {
    button.onclick = async () => {
      const grouped = ["meta_ads", "tiktok_ads", "x_ads", "pinterest_ads", "snapchat_ads"].includes(selected.platform);
      const groupId = grouped ? window.prompt(`${selected.platform} ad set, ad group, line item, or ad squad ID:`) : "";
      if (grouped && !groupId?.trim()) return;
      const examples = {
        google_ads: '{"geo_target_constant_ids":[2840],"language_constant_ids":[1000]}',
        meta_ads: '{"geo_locations":{"countries":["US"]}}',
        linkedin_ads: '{"include":{"and":[{"or":{"urn:li:adTargetingFacet:locations":["urn:li:geo:103644278"]}}]}}',
        x_ads: '{"criteria":[{"targeting_type":"LOCATION","targeting_value":"US"}]}',
        tiktok_ads: '{"location_ids":["US"],"age_groups":["AGE_18_24"]}',
        microsoft_ads: '{"location_ids":["190"]}',
        pinterest_ads: '{"GEO":["US"]}',
        snapchat_ads: '{"geos":[{"country_code":"us"}]}',
      };
      const targetingBehavior = {
        google_ads: "Google replaces the campaign's existing location and language criteria; omitted types are cleared.",
        meta_ads: "Meta replaces the ad set's targeting object.",
        linkedin_ads: "LinkedIn replaces the campaign targeting criteria.",
        tiktok_ads: "TikTok updates only the supplied targeting fields; other fields remain unchanged.",
        x_ads: "X adds these criteria to the line item; remove old criteria in X Ads Manager before adding replacements.",
        microsoft_ads: "Microsoft replaces the campaign's location criteria; other target types remain unchanged.",
        pinterest_ads: "Pinterest replaces the ad group's targeting specification.",
        snapchat_ads: "Snap replaces the ad squad's targeting object.",
      };
      const raw = window.prompt(`Enter ${selected.platform} targeting JSON. ${targetingBehavior[selected.platform] || ""}`, examples[selected.platform] || "{}");
      if (raw === null) return;
      let targeting;
      try { targeting = JSON.parse(raw); }
      catch { toast("Targeting must be valid JSON."); return; }
      if (!window.confirm("Save these targeting settings to the provider?")) return;
      try {
        await api(`/api/ads/platform-accounts/${encodeURIComponent(selected.id)}/campaigns/${encodeURIComponent(button.dataset.adTarget)}/targeting`, { method: "POST", body: { targeting, ad_group_id: groupId } });
        toast("Targeting updated");
        renderAds(root, selected.id);
      } catch (err) { toast(err.message); }
    };
  });
  root.querySelectorAll("button[data-ad-opt]").forEach((button) => {
    button.onclick = async () => {
      const spend = window.prompt(`Pause if 30-day spend reaches this amount in ${campaigns[0]?.currency || "the account currency"}:`, button.dataset.spend || "50");
      if (spend === null || !spend.trim()) return;
      const conversions = window.prompt("Optional: also require conversions to be at or below this count. Leave blank for a spend-only rule.", button.dataset.conversionEnabled === "1" ? button.dataset.conversions : "");
      if (conversions === null) return;
      const condition = conversions.trim() ? ` and ${conversions} or fewer conversions` : " (spend only)";
      if (!window.confirm(`Enable a rule to pause this campaign at ${spend} or more in 30-day spend${condition}?`)) return;
      try {
        await api(`/api/ads/platform-accounts/${encodeURIComponent(selected.id)}/campaigns/${encodeURIComponent(button.dataset.adOpt)}/optimization-rule`, { method: "POST", body: { minimum_30d_spend: spend, maximum_30d_conversions: conversions, conversion_limit_enabled: Boolean(conversions.trim()) } });
        toast("Auto-pause guardrail saved");
        renderAds(root, selected.id);
      } catch (err) { toast(err.message); }
    };
  });
  root.querySelectorAll("button[data-opt-toggle]").forEach((button) => {
    button.onclick = async () => {
      await api(`/api/ads/optimization-rules/${encodeURIComponent(button.dataset.optToggle)}/enabled`, { method: "POST", body: { enabled: button.dataset.enabled === "1" } });
      renderAds(root, selected.id);
    };
  });
}

async function renderAccounts(root) {
  const accounts = await api("/api/accounts");
  const platformConfig = {
    email: { label: "Email", identifier: "Email address", secret: "App password", secretKey: "secret", settings: [["smtp_host", "SMTP host"], ["smtp_port", "SMTP port"], ["daily_cap", "Daily cap"]] },
    whatsapp: { label: "WhatsApp", identifier: "Phone number ID", secret: "Access token", secretKey: "access_token", settings: [["business_account_id", "Business account ID"]] },
    sms: { label: "SMS", identifier: "Sender ID", secret: "Provider API key", secretKey: "secret", settings: [["provider", "Provider"], ["entity_id", "DLT entity ID"], ["template_id", "DLT template ID"]] },
    instagram: { label: "Instagram", identifier: "Professional account ID", secret: "Access token", secretKey: "access_token", settings: [["api_version", "Graph API version (default v25.0)"], ["login_type", "Login type: facebook or instagram"]] },
    facebook: { label: "Facebook Page / Messenger", identifier: "Page ID", secret: "Page access token", secretKey: "access_token", settings: [["api_version", "Graph API version (default v25.0)"]] },
    threads: { label: "Threads", identifier: "Threads user ID", secret: "Threads access token", secretKey: "access_token", settings: [["api_version", "Threads API version (default v1.0)"]] },
    linkedin: { label: "LinkedIn", identifier: "Organization or person URN", secret: "Access token", secretKey: "access_token", settings: [] },
    x: { label: "X", identifier: "Handle", secret: "Access token", secretKey: "access_token", settings: [] },
    tiktok: { label: "TikTok", identifier: "Handle", secret: "Access token", secretKey: "access_token", settings: [] },
    youtube: { label: "YouTube", identifier: "Channel ID", secret: "OAuth access token", secretKey: "access_token", settings: [] },
    meta_ads: { label: "Meta Ads", identifier: "Ad account ID", secret: "Access token", secretKey: "access_token", settings: [["page_id", "Page ID"], ["pixel_id", "Pixel ID"], ["default_sequence", "Default sequence"]] },
    google_ads: { label: "Google Ads (Search, Demand Gen / YouTube)", identifier: "Customer ID", credentials: [["client_id", "Google Cloud OAuth client ID"], ["client_secret", "OAuth client secret"], ["refresh_token", "OAuth refresh token"]], settings: [["login_customer_id", "Manager customer ID (optional)"]] },
    linkedin_ads: { label: "LinkedIn Ads", identifier: "Sponsored account ID", credentials: [["access_token", "Token with rw_ads and r_ads_reporting"]], settings: [["api_version", "Marketing API version (YYYYMM)"], ["campaign_group_urn", "Campaign group URN"], ["currency_code", "Account currency"]] },
    x_ads: { label: "X Ads", identifier: "Ads account ID", credentials: [["consumer_key", "OAuth consumer key"], ["consumer_secret", "OAuth consumer secret"], ["access_token", "OAuth access token"], ["access_token_secret", "OAuth access token secret"]], settings: [["api_version", "X Ads API version (default 12)"], ["funding_instrument_id", "Funding instrument ID"]] },
    tiktok_ads: { label: "TikTok Ads", identifier: "Advertiser ID", credentials: [["access_token", "Access token"]], settings: [["api_version", "Business API version (default v1.3)"]] },
    microsoft_ads: { label: "Microsoft Ads", identifier: "Account ID", credentials: [["access_token", "OAuth access token"], ["refresh_token", "OAuth refresh token"], ["client_id", "OAuth application ID"], ["client_secret", "OAuth application secret"], ["developer_token", "Microsoft Advertising developer token"]], settings: [["customer_id", "Customer ID"], ["time_zone", "Account API time zone"], ["currency_code", "Account currency"]] },
    pinterest_ads: { label: "Pinterest Ads", identifier: "Ad account ID", credentials: [["access_token", "Access token"], ["refresh_token", "Continuous refresh token"], ["client_id", "App ID"], ["client_secret", "App secret"]], settings: [["api_version", "API version (default v5)"]] },
    snapchat_ads: { label: "Snapchat Ads", identifier: "Ad account ID", credentials: [["access_token", "Access token"], ["refresh_token", "Refresh token"], ["client_id", "OAuth app client ID"], ["client_secret", "OAuth app client secret"]], settings: [] },
  };
  const names = { communication: "Communication", social: "Social", ads: "Ads" };
  let editing = null;
  root.innerHTML = `<h1>Accounts</h1>
    <p class="sub">Connect social profiles for publishing and comment automations. For Meta replies, use a professional Instagram account ID or Facebook Page ID with the matching Page/user access token. Credentials are encrypted at rest when the workspace secret key is configured.</p>
    <div class="card"><h3 id="account-form-title">Connect an account</h3>
      <div class="row3"><div><label>Platform</label><select id="account-platform">${Object.entries(platformConfig).map(([key, value]) => `<option value="${key}">${value.label}</option>`).join("")}</select></div>
      <div><label>Account name</label><input id="account-name" placeholder="Sales, Support, Brand main" /></div>
      <div><label id="account-identifier-label">Identifier</label><input id="account-identifier" required /></div></div>
      <div id="account-extra" class="row3"></div>
      <p><button class="btn" id="account-save">Connect account</button> <button class="btn ghost hide" id="account-cancel" type="button">Cancel edit</button></p>
    </div>
    <div id="account-groups"></div>`;
  const platform = $("account-platform");
  const renderExtra = (account = null) => {
    const config = platformConfig[platform.value];
    $("account-identifier-label").textContent = config.identifier;
    const credentialFields = config.credentials || [[config.secretKey, config.secret]];
    const fields = [
      ...credentialFields.filter(([key]) => key).map(([key, label]) => [key, label, "password", "credential"]),
      ...config.settings.map(([key, label]) => [key, label, "text", "setting"]),
    ];
    $("account-extra").innerHTML = fields.map(([key, label, type, kind]) => {
      const inputId = kind === "credential" ? `account-credential-${key}` : `account-${key}`;
      return `<div><label for="${inputId}">${label}</label><input id="${inputId}" type="${type}" autocomplete="${kind === "credential" ? "new-password" : "off"}" /></div>`;
    }).join("");
    if (account) {
      for (const [key] of config.settings) $("account-" + key).value = account.settings?.[key] || "";
    }
  };
  const showAccounts = () => {
    const sections = ["communication", "social", "ads"];
    $("account-groups").innerHTML = sections.map((type) => {
      const rows = accounts.filter((account) => account.account_type === type);
      return `<section><h2>${names[type]}</h2><div class="card account-list">${rows.map((account) => `<div class="account-row">
        <strong>${escapeHtml(account.name)}</strong><span>${escapeHtml(account.platform)} · ${escapeHtml(account.identifier)}</span>
        <span class="account-status ${escapeHtml(account.status)}">${escapeHtml(account.status)}</span>
        <span><button class="btn ghost" type="button" data-edit-account="${escapeHtml(account.id)}">Edit</button> <button class="btn ghost" type="button" data-delete-account="${escapeHtml(account.id)}">Remove</button></span>
      </div>`).join("") || `<p class="sub">No ${names[type].toLowerCase()} accounts connected.</p>`}</div></section>`;
    }).join("");
  };
  platform.onchange = () => renderExtra();
  renderExtra();
  showAccounts();
  $("account-cancel").onclick = () => {
    editing = null;
    $("account-form-title").textContent = "Connect an account";
    $("account-save").textContent = "Connect account";
    $("account-cancel").classList.add("hide");
    $("account-platform").disabled = false;
    $("account-name").value = "";
    $("account-identifier").value = "";
    renderExtra();
  };
  $("account-save").onclick = async () => {
    const config = platformConfig[platform.value];
    const settings = Object.fromEntries(config.settings.map(([key]) => [key, $("account-" + key).value]));
    const credentialFields = config.credentials || [[config.secretKey, config.secret]];
    const credentials = Object.fromEntries(credentialFields.filter(([key]) => key).map(([key]) => [key, $("account-credential-" + key).value]));
    const result = await api("/api/accounts", { method: "POST", body: {
      ...(editing ? { id: editing.id } : {}), platform: platform.value, name: $("account-name").value,
      identifier: $("account-identifier").value, settings, credentials,
    }});
    if (editing) Object.assign(editing, { id: result.id });
    toast("Account saved");
    render();
  };
  $("account-groups").onclick = async (event) => {
    const editButton = event.target.closest("button[data-edit-account]");
    const deleteButton = event.target.closest("button[data-delete-account]");
    if (editButton) {
      editing = accounts.find((account) => account.id === editButton.dataset.editAccount);
      platform.value = editing.platform;
      platform.disabled = true;
      $("account-name").value = editing.name;
      $("account-identifier").value = editing.identifier;
      renderExtra(editing);
      $("account-form-title").textContent = "Edit account";
      $("account-save").textContent = "Save changes";
      $("account-cancel").classList.remove("hide");
    }
    if (deleteButton && window.confirm("Remove this connected account?")) {
      await api(`/api/accounts/${encodeURIComponent(deleteButton.dataset.deleteAccount)}`, { method: "DELETE" });
      toast("Account removed");
      render();
    }
  };
}
