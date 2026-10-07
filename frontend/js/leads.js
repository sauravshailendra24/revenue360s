async function renderLeads(root) {
	const leads = await api("/api/leads");
	const books = await api("/api/sequences");
	root.innerHTML = `<h1>Leads</h1>
		<div class="card">
			<div class="row3">
				<div><label>Name</label><input id="ln" /></div>
				<div><label>Phone</label><input id="lp" /></div>
				<div><label>Email</label><input id="le" /></div>
			</div>
			<div class="row"><div><label>Source</label><input id="ls" value="manual" /></div>
				<div><label>Lead type</label><select id="ltype"><option value="marketing">Marketing lead</option><option value="partner">Partner lead</option><option value="customer">Customer lead</option></select></div>
				<div><label>Tags</label><input id="lt" value="new" /></div></div>
			<p><button class="btn" id="add-lead">Add lead</button></p>
			<label for="lead-import-file">Import CSV or XLSX</label><input type="file" id="lead-import-file" accept=".csv,.xlsx" />
			<div id="import-preview"></div>
		</div>
		<div class="card"><table><tr><th>Name</th><th>Phone</th><th>Email</th><th>Stage</th><th></th></tr>
			${leads.map((lead) => `<tr><td>${escapeHtml(lead.name)}</td><td>${escapeHtml(lead.phone)}</td><td>${escapeHtml(lead.email)}</td><td>${escapeHtml(lead.stage)}</td><td><button class="btn ghost" data-id="${lead.id}">Open</button></td></tr>`).join("")}
		</table></div>
		<div class="card" id="lead-detail"><p class="sub">Open a lead to edit stage, notes, and timeline.</p></div>`;
	$("add-lead").onclick = async () => {
		await api("/api/leads", { method: "POST", body: { name: $("ln").value, phone: $("lp").value, email: $("le").value, source: $("ls").value, tags: $("lt").value + "," + ($("ltype")?.value || "marketing"), sequence_id: books[0]?.id } });
		toast("Lead saved"); render();
	};
	$("lead-import-file").onchange = async (event) => {
		const file = event.target.files[0];
		if (!file) return;
		const form = new FormData();
		form.append("file", file);
		const preview = $("import-preview");
		preview.innerHTML = "<p class='sub'>Reading file…</p>";
		try {
			const grid = await api("/api/leads/import/preview", { method: "POST", body: form });
			drawImportGrid(preview, grid);
		} catch (error) {
			preview.innerHTML = `<p class="sub">${escapeHtml(error.message)}</p>`;
		}
	};
	root.querySelectorAll("button[data-id]").forEach((button) => {
		button.onclick = async () => {
			const data = await api(`/api/leads/${button.dataset.id}/timeline`);
			const lead = data.lead;
			const customFields = Object.entries(lead.custom_fields || {});
			$("lead-detail").innerHTML = `
				<h3>${escapeHtml(lead.name || "Unnamed")}</h3>
				${customFields.length ? `<table><tbody>${customFields.map(([key, value]) => `<tr><th>${escapeHtml(key)}</th><td>${escapeHtml(value)}</td></tr>`).join("")}</tbody></table>` : ""}
				<div class="row"><div><label>Stage</label>
					<select id="lst">${data.stages.map((stage) => `<option ${stage === lead.stage ? "selected" : ""}>${stage}</option>`).join("")}</select></div>
					<div><p><button class="btn" id="save-st">Save stage</button></p></div></div>
				<label>Notes</label><textarea id="lnotes">${escapeHtml(lead.notes || "")}</textarea>
				<p><button class="btn ghost" id="save-notes">Save notes</button></p>
				<table>${data.messages.map((message) => `<tr><td>${escapeHtml(message.created_at)}</td><td>${escapeHtml(message.channel)} ${escapeHtml(message.direction)}</td><td>${escapeHtml(message.body || "")}</td></tr>`).join("")}</table>`;
			$("save-st").onclick = async () => { await api(`/api/leads/${lead.id}/stage`, { method: "POST", body: { stage: $("lst").value } }); toast("Stage saved"); render(); };
			$("save-notes").onclick = async () => { await api(`/api/leads/${lead.id}/notes`, { method: "POST", body: { notes: $("lnotes").value } }); toast("Notes saved"); render(); };
		};
	});
}

function drawImportGrid(container, grid) {
	const columns = grid.columns || [];
	const rows = grid.rows || [];
	const renderGrid = () => {
		container.innerHTML = `<div class="import-toolbar">
			<span class="sub">${rows.length} rows · ${columns.length} columns</span>
			<button class="btn ghost" type="button" data-grid-action="add-column">Add column</button>
			<button class="btn ghost" type="button" data-grid-action="add-row">Add row</button>
			<button class="btn" type="button" data-grid-action="save">Save leads</button>
		</div>
		<div class="import-grid-scroll"><table class="import-grid"><thead><tr>${columns.map((column, index) => `<th><input data-column="${index}" aria-label="Rename column ${index + 1}" value="${escapeHtml(column)}" /></th>`).join("")}<th></th></tr></thead>
		<tbody>${rows.map((row, rowIndex) => `<tr>${columns.map((column, columnIndex) => `<td><input data-row="${rowIndex}" data-cell="${columnIndex}" aria-label="Row ${rowIndex + 1}, ${escapeHtml(column)}" value="${escapeHtml(row[columnIndex] || "")}" /></td>`).join("")}<td><button class="btn ghost" type="button" data-grid-action="remove-row" data-row="${rowIndex}">Remove</button></td></tr>`).join("")}</tbody></table></div>
		<div class="import-status" aria-live="polite"></div>`;
	};
	renderGrid();
	container.oninput = (event) => {
		const input = event.target;
		if (input.hasAttribute("data-column")) columns[Number(input.dataset.column)] = input.value;
		if (input.hasAttribute("data-cell")) rows[Number(input.dataset.row)][Number(input.dataset.cell)] = input.value;
	};
	container.onclick = async (event) => {
		const button = event.target.closest("button[data-grid-action]");
		if (!button) return;
		const action = button.dataset.gridAction;
		if (action === "add-row") rows.push(columns.map(() => ""));
		if (action === "add-column") {
			columns.push(`Column ${columns.length + 1}`);
			rows.forEach((row) => row.push(""));
		}
		if (action === "remove-row") rows.splice(Number(button.dataset.row), 1);
		if (action === "save") {
			button.disabled = true;
			try {
				const result = await api("/api/leads/import", { method: "POST", body: { columns, rows } });
				const status = container.querySelector(".import-status");
				const details = result.errors.slice(0, 5).map((item) => `Row ${item.row}: ${item.message}`).join("; ");
				status.textContent = `Saved ${result.created} new, updated ${result.updated}, skipped ${result.skipped}. ${result.errors.length} row errors.${details ? ` ${details}` : ""}`;
				toast(`Saved ${result.created} new and updated ${result.updated} leads`);
				if (!result.errors.length) render();
			} catch (error) {
				container.querySelector(".import-status").textContent = error.message;
			} finally {
				if (button.isConnected) button.disabled = false;
			}
			return;
		}
		renderGrid();
	};
}
