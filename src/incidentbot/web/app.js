// incidentbot UI: plain ES module, no build step. Hash routes:
// #/chat[/<channel id>], #/incidents, #/incident/<id>, #/status, #/metrics
const app = document.getElementById("app");
const USERS = ["hiro", "grace", "kofi", "ana", "ben", "chioma", "elif"];
const COLORS = ["#2a78d6", "#0ca30c", "#c98a00", "#d03b3b", "#8a5cd1", "#e0662f", "#1f9e9e"];
const STATUS = {
  operational: ["Operational", "var(--good)"], degraded_performance: ["Degraded performance", "var(--warning)"],
  partial_outage: ["Partial outage", "var(--orange)"], major_outage: ["Major outage", "var(--critical)"],
};
let timer = null;
let me = localGet("user") || "hiro";

function localGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const get = (url) => fetch(url).then((r) => { if (!r.ok) throw new Error(`${url}: ${r.status}`); return r.json(); });
const post = (url, body) => fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }).then((r) => r.json());
const hhmm = (ts) => (ts ? new Date(ts * 1000).toISOString().slice(11, 16) : "n/a");
const day = (ts) => (ts ? new Date(ts * 1000).toISOString().slice(0, 10) : "n/a");
const fmtMin = (m) => (m == null ? "n/a" : m < 120 ? `${Math.round(m)} min` : `${(m / 60).toFixed(1)} h`);
const pct = (v) => (v == null ? "n/a" : `${Math.round(v * 100)}%`);
const sev = (s) => `<span class="pill ${s}">${esc(s)}</span>`;
const st = (s) => `<span class="pill st-${s}">${esc(s)}</span>`;
const color = (u) => COLORS[[...u].reduce((a, c) => a + c.charCodeAt(0), 0) % COLORS.length];

// Minimal mrkdwn: *bold*, `code`, >quote lines, @mentions, #channels.
function mrkdwn(text) {
  return esc(text).split("\n").map((line) => {
    let l = line.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*([^*\n]+)\*/g, "<b>$1</b>")
      .replace(/(^|\s)@([a-z0-9_.-]+)/gi, '$1<b>@$2</b>');
    if (l.startsWith("&gt;")) l = `<blockquote>${l.slice(4)}</blockquote>`;
    return l;
  }).join("\n").replace(/<\/blockquote>\n/g, "</blockquote>");
}

function route() {
  clearInterval(timer);
  const [, name = "chat", arg] = location.hash.split("/");
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.r === name || (name === "incident" && a.dataset.r === "incidents")));
  const views = { chat, incidents, incident, status, metrics };
  (views[name] || chat)(arg).catch((e) => { app.innerHTML = `<div class="card">Error: ${esc(e.message)}</div>`; });
}
window.addEventListener("hashchange", route);

// chat -------------------------------------------------------------------------------
async function chat(channelId) {
  const { channels } = await get("/api/channels");
  const open = channels.filter((c) => c.incident_id && c.state !== "resolved");
  if (!channelId) channelId = (open[open.length - 1] || channels[0]).id;
  const ch = channels.find((c) => c.id === channelId) || channels[0];
  const link = (c) => `<a href="#/chat/${c.id}" class="${c.id === ch.id ? "active" : ""}">${c.severity ? sev(c.severity) : "#"} ${esc(c.name)}</a>`;
  const general = channels.filter((c) => !c.incident_id);
  const resolved = channels.filter((c) => c.incident_id && c.state === "resolved").reverse();
  app.innerHTML = `<div class="chat">
    <div class="side"><h3>Channels</h3>${general.map(link).join("")}
      <h3>Open incidents</h3>${open.map(link).join("") || '<span class="muted">none</span>'}
      <h3>Resolved</h3>${resolved.map(link).join("")}</div>
    <div class="pane">
      <div class="pane-head"><b>#${esc(ch.name)}</b><span class="muted">${esc(ch.topic)}</span>
        ${ch.incident_id ? `<a href="#/incident/${ch.incident_id}">${esc(ch.incident_id)} details</a>` : ""}</div>
      <div class="msgs" id="msgs"></div>
      <div class="hints">${hints(ch).map((h) => `<button data-hint="${esc(h)}">${esc(h)}</button>`).join("")}</div>
      <form class="composer" id="composer">
        <select id="me" aria-label="Post as">${USERS.map((u) => `<option ${u === me ? "selected" : ""}>${u}</option>`).join("")}</select>
        <input id="text" autocomplete="off" placeholder="Message #${esc(ch.name)} or /incident ..." aria-label="Message">
        <button class="primary" type="submit">Send</button>
      </form>
    </div></div>`;
  const box = document.getElementById("msgs");
  const input = document.getElementById("text");
  document.getElementById("me").onchange = (e) => { me = e.target.value; localSet("user", me); };
  document.querySelectorAll("[data-hint]").forEach((b) => { b.onclick = () => { input.value = b.dataset.hint; input.focus(); }; });
  document.getElementById("composer").onsubmit = async (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    await post("/api/chat/message", { user: me, channel_id: ch.id, text });
    if (text.startsWith("/incident declare")) return route();
    await draw(true);
  };
  let last = "";
  async function draw(force) {
    const { messages } = await get(`/api/channels/${ch.id}/messages`);
    const sig = JSON.stringify(messages.map((m) => [m.id, m.blocks.length, m.reactions]));
    if (sig === last && !force) return;
    last = sig;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    box.innerHTML = messages.map(renderMsg).join("");
    box.querySelectorAll("[data-action]").forEach((b) => {
      b.onclick = async () => {
        await post("/api/chat/interaction", { user: me, channel_id: ch.id, message_id: b.dataset.msg, action_id: b.dataset.action, value: b.dataset.value });
        draw(true);
      };
    });
    box.querySelectorAll("[data-star]").forEach((b) => {
      b.onclick = async () => { await post("/api/chat/reaction", { user: me, channel_id: ch.id, message_id: b.dataset.star, reaction: "star" }); draw(true); };
    });
    if (atBottom || force) box.scrollTop = box.scrollHeight;
  }
  await draw(true);
  timer = setInterval(() => draw(false), 3000);
}

function hints(ch) {
  if (!ch.incident_id) return ['/incident declare sev2 "search latency high" service=search', "/incident help"];
  if (ch.state === "resolved") return ["/incident timeline", "/incident done A1"];
  return ['/incident update "..."', "/incident state monitoring", "/incident status", "/incident runbooks", "/incident handoff commander @ana", "/incident resolve"];
}

function renderMsg(m) {
  const blocks = m.blocks.map((b) => {
    if (b.type === "section") return `<div class="block-section">${mrkdwn(b.text.text)}</div>`;
    if (b.type === "context") return `<div class="block-context">${b.elements.map((e) => mrkdwn(e.text)).join(" ")}</div>`;
    if (b.type === "actions") return `<div class="block-actions">${b.elements.map((e) => `<button class="${e.style || ""}" data-msg="${m.id}" data-action="${esc(e.action_id)}" data-value="${esc(e.value)}">${esc(e.text.text)}</button>`).join("")}</div>`;
    return "";
  }).join("");
  const isCmd = !m.bot && m.text.startsWith("/");
  const body = blocks || `<div class="body ${isCmd ? "cmd" : ""}">${isCmd ? esc(m.text) : mrkdwn(m.text)}</div>`;
  const reacts = Object.entries(m.reactions).map(([r, us]) => `<span class="react ${us.includes(me) ? "on" : ""}" title="${esc(us.join(", "))}">${r === "star" ? "&#9733;" : esc(r)} ${us.length}</span>`).join("");
  const name = m.bot ? "incidentbot" : m.user;
  const av = m.bot ? "var(--bot)" : color(m.user);
  return `<div class="msg ${m.thread_ts ? "thread" : ""}">
    <div class="avatar" style="background:${av}">${esc(name[0].toUpperCase())}</div>
    <div><div class="who">${esc(name)}<span class="muted">${hhmm(m.ts)}${m.bot ? " APP" : ""}</span></div>${body}
      <div class="reacts">${reacts}${m.bot || isCmd ? "" : `<button class="react star" data-star="${m.id}" title="Star: add to the incident timeline">&#9734; star</button>`}</div></div></div>`;
}

// incidents --------------------------------------------------------------------------
async function incidents() {
  const { incidents: rows } = await get("/api/incidents");
  app.innerHTML = `<h1>Incidents <span class="muted">${rows.length} total, ${rows.filter((r) => r.state !== "resolved").length} open</span></h1>
    <div class="card scroll"><table><thead><tr><th>Incident</th><th>Severity</th><th>State</th><th>Service</th><th>Detected (UTC)</th>
    <th class="num">Time to resolve</th><th>Commander</th><th class="num">Action items</th></tr></thead><tbody>
    ${rows.map((r) => `<tr><td class="wrap"><a href="#/incident/${r.id}">${r.id}</a> ${esc(r.title)}</td><td>${sev(r.severity)}</td><td>${st(r.state)}</td>
      <td>${esc(r.service)}</td><td>${day(r.detected_at)} ${hhmm(r.detected_at)}</td>
      <td class="num">${r.resolved_at ? fmtMin((r.resolved_at - r.detected_at) / 60) : "open"}</td><td>@${esc(r.roles.commander)}</td>
      <td class="num">${r.action_items_total - r.action_items_open}/${r.action_items_total} done</td></tr>`).join("")}</tbody></table></div>`;
}

async function incident(id) {
  const d = await get(`/api/incidents/${id}`);
  const dur = (a, b) => (a && b ? fmtMin((b - a) / 60) : "n/a");
  const roles = [["commander", "Incident commander"], ["comms", "Communications lead"], ["ops", "Ops lead"]];
  const runs = d.runbook_runs.map((r) => `<div class="card"><b>${r.id}</b> ${esc(r.runbook)} ${r.dry_run ? '<span class="pill">dry run</span>' : ""}
      <span class="pill">${esc(r.status.replace(/_/g, " "))}</span> <span class="muted">by @${esc(r.actor)} at ${hhmm(r.created_at)}</span>
      <ol class="steps">${r.steps.map((s) => `<li><b>${esc(s.status.replace(/_/g, " "))}</b> ${esc(s.description)}: ${esc(s.result || s.summary)}${s.by ? ` <span class="muted">(@${esc(s.by)})</span>` : ""}</li>`).join("")}</ol></div>`).join("");
  let pm = "";
  if (d.has_postmortem) {
    const md = await fetch(`/api/incidents/${id}/postmortem?format=markdown`).then((r) => r.text());
    pm = `<h2>Postmortem draft</h2><div class="card pm">${mdToHtml(md)}</div>`;
  }
  app.innerHTML = `<h1>${d.id} ${esc(d.title)}</h1>
    <p>${sev(d.severity)} ${st(d.state)} <span class="muted">service ${esc(d.service)}, declared by @${esc(d.declared_by)} on ${day(d.declared_at)}.</span>
      <a href="#/chat/${d.channel_id}">Open the channel</a></p>
    <div class="roles">${roles.map(([k, t]) => `<div class="stat"><span>${t}</span><b>${d.roles[k] ? "@" + esc(d.roles[k]) : "unassigned"}</b></div>`).join("")}</div>
    <h2>Key times (UTC)</h2>
    <div class="stats">
      <div class="stat"><span>Detected</span><b>${hhmm(d.detected_at)}</b></div>
      <div class="stat"><span>Time to acknowledge</span><b>${dur(d.detected_at, d.declared_at)}</b></div>
      <div class="stat"><span>Time to mitigate</span><b>${dur(d.detected_at, d.mitigated_at)}</b></div>
      <div class="stat"><span>Time to resolve</span><b>${dur(d.detected_at, d.resolved_at)}</b></div>
      <div class="stat"><span>Affected users</span><b>${d.affected_users.toLocaleString("en-US")}</b></div>
      <div class="stat"><span>Updates / reminders</span><b>${d.updates.length} / ${d.reminders.length}</b></div>
    </div>
    <h2>Timeline <span class="muted">${d.timeline.length} entries. Shaded rows came from the event bus.</span></h2>
    <div class="card scroll"><table><thead><tr><th>UTC</th><th>Id</th><th>Kind</th><th>Actor</th><th>Entry</th></tr></thead><tbody>
      ${d.timeline.map((e) => `<tr class="src-${e.source}"><td class="num">${hhmm(e.ts)}</td><td>${e.id}</td><td class="kind">${esc(e.kind)}</td><td>${esc(e.actor)}</td><td class="wrap">${esc(e.text)}</td></tr>`).join("")}
    </tbody></table></div>
    <h2>Runbook runs <span class="muted">${d.audit.length} audit entries</span></h2>
    <div class="grid2">${runs || '<div class="card muted">No runbooks run.</div>'}</div>
    <h2>Action items</h2>
    <div class="card scroll"><table><thead><tr><th>Id</th><th>Item</th><th>Owner</th><th>Source</th><th>Done</th></tr></thead><tbody>
      ${d.action_items.map((a) => `<tr><td>${a.id}</td><td class="wrap">${esc(a.text)}</td><td>${a.owner ? "@" + esc(a.owner) : ""}</td><td>${a.source_entry_id}</td><td>${a.done ? day(a.done_at) : "open"}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">None</td></tr>'}
    </tbody></table></div>
    <h2>Status updates</h2>
    <div class="card scroll"><table><thead><tr><th>Id</th><th>Visibility</th><th>State</th><th>Status</th><th>Text</th></tr></thead><tbody>
      ${d.status_updates.map((u) => `<tr><td>${u.id}</td><td>${u.visibility}</td><td>${u.state}</td><td>${u.status.replace(/_/g, " ")}${u.approved_by ? ` by @${esc(u.approved_by)}` : ""}</td><td class="wrap">${esc(u.body)}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">None</td></tr>'}
    </tbody></table></div>${pm}`;
}

function mdToHtml(md) {
  const out = [];
  let list = false;
  for (const line of md.split("\n")) {
    const inline = (s) => esc(s).replace(/\[(T\d+(?:, T\d+)*)\]/g, '<span class="muted">[$1]</span>');
    if (line.startsWith("- ")) { if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(line.slice(2))}</li>`); continue; }
    if (list) { out.push("</ul>"); list = false; }
    if (line.startsWith("# ")) out.push(`<h1>${inline(line.slice(2))}</h1>`);
    else if (line.startsWith("## ")) out.push(`<h2>${inline(line.slice(3))}</h2>`);
    else if (line.startsWith("> ")) out.push(`<blockquote>${inline(line.slice(2))}</blockquote>`);
    else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push("</ul>");
  return out.join("");
}

// status page ------------------------------------------------------------------------
async function status() {
  const d = await get("/api/status");
  const [label, col] = STATUS[d.overall];
  const head = d.overall === "operational" ? "All systems operational" : `${label} on some components`;
  app.innerHTML = `<h1>Tillwave status <span class="muted">public page preview. Only updates approved by a comms lead appear here.</span></h1>
    <div class="overall" style="background:${col}">${head}</div>
    <h2>Components</h2><div class="card">${d.components.map((c) => `<div class="comp"><span>${esc(c.name)}</span>
      <span class="legend"><span class="dot" style="background:${STATUS[c.status][1]}"></span>${STATUS[c.status][0]}</span></div>`).join("")}</div>
    <h2>Recent incidents</h2>
    ${d.incidents.map((i) => `<div class="card" style="margin-bottom:10px"><b>${esc(i.title)}</b> <span class="muted">${esc(i.component)}, ${day(i.opened_at)}</span>
      ${i.updates.map((u) => `<div class="upd"><b>${esc(u.state)}</b> <span class="muted">${day(u.published_at)} ${hhmm(u.published_at)} UTC</span><div>${esc(u.body)}</div></div>`).join("")}</div>`).join("") || '<div class="card muted">No public incidents.</div>'}`;
}

// metrics ----------------------------------------------------------------------------
function bars(title, rows, opts = {}) {
  // Vertical bars, one y-axis. rows: [{label, value, text}]
  const W = 520, H = 230, L = 44, R = 10, T = 14, B = 34;
  const max = Math.max(opts.max || 0, ...rows.map((r) => r.value), 1);
  const step = niceStep(max);
  const top = Math.ceil(max / step) * step;
  const y = (v) => T + (H - T - B) * (1 - v / top);
  const bw = (W - L - R) / rows.length;
  let g = "";
  for (let v = 0; v <= top + 1e-9; v += step) g += `<line class="gl" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${opts.fmt ? opts.fmt(v) : v}</text>`;
  const every = Math.ceil(rows.length / 14);
  const b = rows.map((r, i) => `<rect class="bar" x="${L + i * bw + bw * 0.15}" y="${y(r.value)}" width="${bw * 0.7}" height="${y(0) - y(r.value)}"><title>${esc(r.label)}: ${esc(r.text ?? r.value)}</title></rect>
    ${rows.length <= 8 ? `<text class="val" x="${L + i * bw + bw / 2}" y="${y(r.value) - 4}" text-anchor="middle">${esc(r.text ?? r.value)}</text>` : ""}
    ${i % every === 0 ? `<text x="${L + i * bw + bw / 2}" y="${H - B + 16}" text-anchor="middle">${esc(r.label)}</text>` : ""}`).join("");
  return `<div class="card chart"><b>${esc(title)}</b>${opts.note ? `<div class="muted">${esc(opts.note)}</div>` : ""}
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">${g}<line class="axis" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/>${b}</svg></div>`;
}
function hbars(title, rows) {
  // Horizontal bars, one value axis along the bottom.
  const W = 520, L = 110, R = 30, T = 6, rowH = 22, B = 24, H = T + rows.length * rowH + B;
  const max = Math.max(...rows.map((r) => r.value), 1);
  const step = niceStep(max), top = Math.ceil(max / step) * step;
  const x = (v) => L + (W - L - R) * (v / top);
  let g = "";
  for (let v = 0; v <= top + 1e-9; v += step) g += `<line class="gl" x1="${x(v)}" x2="${x(v)}" y1="${T}" y2="${H - B}"/><text x="${x(v)}" y="${H - B + 15}" text-anchor="middle">${v}</text>`;
  const b = rows.map((r, i) => `<text x="${L - 8}" y="${T + i * rowH + 15}" text-anchor="end">${esc(r.label)}</text>
    <rect class="bar" x="${L}" y="${T + i * rowH + 4}" width="${x(r.value) - L}" height="${rowH - 8}"><title>${esc(r.label)}: ${r.value}</title></rect>
    <text class="val" x="${x(r.value) + 4}" y="${T + i * rowH + 15}">${r.value}</text>`).join("");
  return `<div class="card chart"><b>${esc(title)}</b><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">${g}${b}</svg></div>`;
}
function niceStep(max) {
  const raw = max / 4, p = 10 ** Math.floor(Math.log10(raw)), n = raw / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
}

async function metrics() {
  const d = await get("/api/metrics/summary");
  const o = d.overall, ai = d.action_items;
  const sevs = Object.entries(d.by_severity);
  app.innerHTML = `<h1>Incident metrics <span class="muted">${d.total} incidents: the scripted quarter (July to September 2026) plus anything declared live</span></h1>
    <div class="stats">
      <div class="stat"><span>Incidents</span><b>${d.total}</b></div>
      <div class="stat"><span>Median time to acknowledge</span><b>${fmtMin(o.tta.median)}</b></div>
      <div class="stat"><span>Median time to mitigate</span><b>${fmtMin(o.ttm.median)}</b></div>
      <div class="stat"><span>Median time to resolve</span><b>${fmtMin(o.ttr.median)}</b></div>
      <div class="stat"><span>Update cadence adherence</span><b>${pct(o.cadence_adherence)}</b></div>
      <div class="stat"><span>Action items done</span><b>${ai.done}/${ai.total}</b></div>
    </div>
    <h2>Trends</h2>
    <div class="grid2">
      ${bars("Incidents per week, July to September", d.weekly.filter((w) => w.week <= 13).map((w) => ({ label: `W${w.week}`, value: w.count, text: `${w.count} (week of ${day(w.start)})` })))}
      ${hbars("Incidents by service", Object.entries(d.by_service).map(([s, n]) => ({ label: s, value: n })))}
      ${bars("Median time to resolve by severity (minutes)", sevs.map(([s, b]) => ({ label: s, value: b.ttr.median || 0, text: `${Math.round(b.ttr.median || 0)}` })))}
      ${bars("Update cadence adherence by severity (%)", sevs.filter(([, b]) => b.cadence_adherence != null).map(([s, b]) => ({ label: s, value: Math.round(b.cadence_adherence * 100), text: `${Math.round(b.cadence_adherence * 100)}%` })), { max: 100, note: "Share of gaps between updates within the policy: sev1 15 min, sev2 30, sev3 60" })}
    </div>
    <h2>By severity</h2>
    <div class="card scroll"><table><thead><tr><th>Severity</th><th class="num">Incidents</th><th class="num">Median TTA</th><th class="num">Median TTM</th>
      <th class="num">Median TTR</th><th class="num">p90 TTR</th><th class="num">Cadence</th><th class="num">Reminders</th></tr></thead><tbody>
      ${sevs.map(([s, b]) => `<tr><td>${sev(s)}</td><td class="num">${b.count}</td><td class="num">${fmtMin(b.tta.median)}</td><td class="num">${fmtMin(b.ttm.median)}</td>
        <td class="num">${fmtMin(b.ttr.median)}</td><td class="num">${fmtMin(b.ttr.p90)}</td><td class="num">${pct(b.cadence_adherence)}</td><td class="num">${b.reminders}</td></tr>`).join("")}
    </tbody></table></div>
    <p class="muted">Action item completion ${pct(ai.completion)}, median ${ai.median_days_to_close ?? "n/a"} days to close.
      Durations are measured from the first alert. Time to mitigate ends at the first move to monitoring.</p>`;
}

route();
