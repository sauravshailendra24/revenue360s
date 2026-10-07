async function renderEmail(root) {
  const [data, accounts] = await Promise.all([api("/api/email"), api("/api/accounts")]);
  const c = data.connection;
  root.innerHTML = `<h1>Email</h1>
    <p class="sub">Delivery mode: <b style="font-size:16px">${escapeHtml(data.mode)}</b> · ${data.used} / ${c.daily_cap} sent this UTC day.</p>
    <div class="card">
      <h3>Gmail / SMTP</h3>
      <div class="row"><div><label>From address</label><input id="em-addr" value="${escapeHtml(c.email_address)}" /></div>
      <div><label>App password</label><input id="em-sec" type="password" /></div></div>
      <div class="row"><div><label>Daily cap</label><input id="em-cap" type="number" value="${c.daily_cap}" /></div>
      <div><p class="sub">${c.has_secret ? "App password already saved." : "No app password yet — sends stay in sandbox."}</p></div></div>
      <p><button class="btn" id="em-conn">Save connection</button></p>
    </div>
    <div class="card">
      <h3>Compose</h3>
      <label>To</label><input id="em-to" />
      <label>Send from</label><select id="em-account"><option value="">Workspace default</option>${accounts.filter((account) => account.platform === "email").map((account) => `<option value="${escapeHtml(account.id)}">${escapeHtml(account.name)} · ${escapeHtml(account.identifier)}</option>`).join("")}</select>
      <label>Subject</label><input id="em-subj" />
      <label>Body</label><textarea id="em-body"></textarea>
      <p><button class="btn" id="em-send">Send email</button></p>
    </div>
    <div class="card"><h3>Log</h3>
      <table>${data.log.map((r) => `<tr><td>${escapeHtml(r.created_at)}</td><td>${escapeHtml(r.recipient)}</td><td>${escapeHtml(r.status)}</td><td>${escapeHtml(r.subject)}</td></tr>`).join("")}</table>
    </div>`;
  $("em-conn").onclick = async () => {
    await api("/api/email/connection", { method: "POST", body: { email_address: $("em-addr").value, secret: $("em-sec").value, daily_cap: $("em-cap").value } });
    toast("Email connection saved"); render();
  };
  $("em-send").onclick = async () => {
    const res = await api("/api/email/send", { method: "POST", body: { to: $("em-to").value, subject: $("em-subj").value, body: $("em-body").value, account_id: $("em-account").value } });
    toast(res.ok ? "Sent or sandboxed" : "Failed / suppressed / cap");
    render();
  };
}

async function renderWhatsapp(root) {
  const [data, accounts] = await Promise.all([api("/api/whatsapp"), api("/api/accounts")]);
  const c = data.connection;
  root.innerHTML = `<h1>WhatsApp</h1>
    <p class="sub">Delivery mode: <b style="font-size:16px">${escapeHtml(data.mode)}</b>. Inbound from Meta uses the public webhook.</p>
    <div class="card">
      <div class="row3">
        <div><label>Phone number ID</label><input id="wa-id" value="${escapeHtml(c.phone_number_id)}" /></div>
        <div><label>Business account ID</label><input id="wa-biz" value="${escapeHtml(c.business_account_id)}" /></div>
        <div><label>Access token</label><input id="wa-tok" type="password" /></div>
      </div>
      <p><button class="btn" id="wa-conn">Connect WhatsApp</button></p>
      <p class="sub">Webhook: <code>${escapeHtml(data.webhook)}</code> · token ${escapeHtml(data.verify_token)}</p>
    </div>
    <div class="card">
      <label>Recipient number</label><input id="wa-to" />
      <label>Send from</label><select id="wa-account"><option value="">Workspace default</option>${accounts.filter((account) => account.platform === "whatsapp").map((account) => `<option value="${escapeHtml(account.id)}">${escapeHtml(account.name)} · ${escapeHtml(account.identifier)}</option>`).join("")}</select>
      <label>Customer name (inbound)</label><input id="wa-name" />
      <label>Message</label><textarea id="wa-body"></textarea>
      <p><button class="btn" id="wa-send">Send WhatsApp</button>
      <button class="btn ghost" id="wa-in">Record inbound</button></p>
    </div>
    <div class="card"><table>${data.log.map((r) => `<tr><td>${escapeHtml(r.created_at)}</td><td>${escapeHtml(r.recipient)}</td><td>${escapeHtml(r.status)}</td><td>${escapeHtml(r.body)}</td></tr>`).join("")}</table></div>`;
  $("wa-conn").onclick = async () => {
    await api("/api/whatsapp/connection", { method: "POST", body: { phone_number_id: $("wa-id").value, business_account_id: $("wa-biz").value, access_token: $("wa-tok").value } });
    toast("WhatsApp saved"); render();
  };
  $("wa-send").onclick = async () => {
    toast(await api("/api/whatsapp/send", { method: "POST", body: { phone: $("wa-to").value, body: $("wa-body").value, account_id: $("wa-account").value } }));
    render();
  };
  $("wa-in").onclick = async () => {
    toast(await api("/api/whatsapp/inbound", { method: "POST", body: { sender: $("wa-to").value, body: $("wa-body").value, name: $("wa-name").value } }));
    render();
  };
}

async function renderSms(root) {
  const [data, accounts] = await Promise.all([api("/api/sms"), api("/api/accounts")]);
  const c = data.connection;
  root.innerHTML = `<h1>SMS</h1>
    <p class="sub">Delivery mode: <b style="font-size:16px">${escapeHtml(data.mode)}</b>. Live send needs MSG91 key + sender + DLT template.</p>
    <div class="card">
      <div class="row3">
        <div><label>Sender ID</label><input id="sms-sid" value="${escapeHtml(c.sender_id)}" /></div>
        <div><label>DLT template ID</label><input id="sms-tpl" value="${escapeHtml(c.template_id)}" /></div>
        <div><label>API key</label><input id="sms-sec" type="password" /></div>
      </div>
      <label>DLT Entity ID</label><input id="sms-ent" value="${escapeHtml(c.entity_id)}" />
      <p><button class="btn" id="sms-conn">Save SMS connection</button></p>
    </div>
    <div class="card">
      <label>Recipient number</label><input id="sms-to" />
      <label>Send from</label><select id="sms-account"><option value="">Workspace default</option>${accounts.filter((account) => account.platform === "sms").map((account) => `<option value="${escapeHtml(account.id)}">${escapeHtml(account.name)} · ${escapeHtml(account.identifier)}</option>`).join("")}</select>
      <label>Message</label><textarea id="sms-body" maxlength="160"></textarea>
      <p><button class="btn" id="sms-send">Send SMS</button>
      <button class="btn ghost" id="sms-in">Record inbound</button></p>
    </div>
    <div class="card"><table>${data.log.map((r) => `<tr><td>${escapeHtml(r.created_at)}</td><td>${escapeHtml(r.recipient)}</td><td>${escapeHtml(r.status)}</td><td>${escapeHtml(r.body)}</td></tr>`).join("")}</table></div>`;
  $("sms-conn").onclick = async () => {
    await api("/api/sms/connection", { method: "POST", body: { sender_id: $("sms-sid").value, template_id: $("sms-tpl").value, entity_id: $("sms-ent").value, secret: $("sms-sec").value } });
    toast("SMS saved"); render();
  };
  $("sms-send").onclick = async () => {
    toast(await api("/api/sms/send", { method: "POST", body: { phone: $("sms-to").value, body: $("sms-body").value, account_id: $("sms-account").value } }));
    render();
  };
  $("sms-in").onclick = async () => {
    toast(await api("/api/sms/inbound", { method: "POST", body: { sender: $("sms-to").value, body: $("sms-body").value } }));
    render();
  };
}

async function renderInbox(root) {
  const [threads, accounts] = await Promise.all([api("/api/inbox"), api("/api/accounts")]);
  root.innerHTML = `<h1>Inbox</h1><p class="sub">Conversations stay with the lead across communication channels.</p>
    <div class="row"><div class="card" id="thread-list">${threads.map((t, i) => `<p><button class="btn ghost" data-i="${i}">${escapeHtml(t.name || t.email || t.phone || "Unnamed lead")} · ${escapeHtml(t.channels || "")}</button></p>`).join("") || "No conversations yet."}</div>
    <div class="card" id="thread-view"><p class="sub">Pick a thread</p></div></div>`;
  root.querySelectorAll("#thread-list button").forEach((btn) => {
    btn.onclick = async () => {
      const t = threads[Number(btn.dataset.i)];
      const data = await api(`/api/inbox/thread?lead_id=${encodeURIComponent(t.lead_id)}`);
      const channels = (t.channels || "").split(",").filter((channel) => ["email", "sms", "whatsapp"].includes(channel));
      const selectedChannel = channels[0] || "whatsapp";
      const accountOptions = (channel) => accounts.filter((account) => account.platform === channel).map((account) => `<option value="${escapeHtml(account.id)}">${escapeHtml(account.name)} · ${escapeHtml(account.identifier)}</option>`).join("");
      $("thread-view").innerHTML = `
        <p class="sub">${escapeHtml(data.lead.name || "Unnamed lead")} · ${escapeHtml(data.lead.stage)}</p>
        <div class="thread">${data.messages.map((m) => `<div class="bubble ${m.direction === "inbound" ? "in" : ""}"><b>${escapeHtml(m.channel)} · ${escapeHtml(m.direction)}</b><p>${escapeHtml(m.body)}</p><small>${escapeHtml(m.created_at)}</small></div>`).join("")}</div>
        <label>Channel</label><select id="reply-channel">${channels.map((channel) => `<option ${channel === selectedChannel ? "selected" : ""}>${channel}</option>`).join("")}</select>
        <label>Reply from account</label><select id="reply-account"><option value="">Workspace default</option>${accountOptions(selectedChannel)}</select>
        <label>Reply</label><textarea id="reply-body"></textarea>
        <p><button class="btn" id="send-reply">Send</button> <button class="btn ghost" id="sim-in">Record inbound</button></p>`;
      const replyAccount = $("reply-account");
      $("reply-channel").onchange = () => { replyAccount.innerHTML = `<option value="">Workspace default</option>${accountOptions($("reply-channel").value)}`; };
      $("send-reply").onclick = async () => {
        await api("/api/inbox/send", { method: "POST", body: { lead_id: data.lead.id, channel: $("reply-channel").value, body: $("reply-body").value, account_id: replyAccount.value } });
        toast("Sent"); render();
      };
      $("sim-in").onclick = async () => {
        const channel = $("reply-channel").value;
        await api("/api/inbox/inbound", { method: "POST", body: { channel, sender: channel === "email" ? data.lead.email : data.lead.phone, body: $("reply-body").value, name: data.lead.name } });
        toast("Inbound saved"); render();
      };
    };
  });
}


