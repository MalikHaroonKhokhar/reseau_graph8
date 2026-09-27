// Réseau dashboard (HAR-106): Start My Day, the daily report and Ask Réseau, every claim linked to its evidence.
// No build step: the browser loads this module as is and talks only to reseau/dashboard.py. Server strings are
// set as text, never as HTML, and nothing is logged to the console: briefings carry customer and deal data.

const SECTIONS = {
  today: [["focus", "Focus today"], ["needs_attention", "Needs attention"], ["yesterday", "Yesterday"]],
  report: [["completed", "Completed"], ["merged", "Merged"], ["commits", "Commits"], ["blocked", "Blocked"]],
};
const SURFACES = ["today", "report", "ask"];
const NOTHING = "Nothing to report.";
const UPSTREAMS = { github: "GitHub", linear: "Linear", graph8: "Graph8" };
const KINDS = {
  "github:pr": "GitHub pull request", "github:commit": "GitHub commit",
  "github:review_comment": "GitHub review comment", "github:review": "GitHub review", "linear:issue": "Linear issue",
  "graph8:customer": "Graph8 customer", "graph8:opportunity": "Graph8 opportunity",
  "graph8:commitment": "Graph8 commitment", "graph8:conversation": "Graph8 conversation",
};
const ERRORS = {
  unverified: "Graph8's reply didn't pass verification", workflow_failed: "The workflow failed",
  not_configured: "Not set up yet", invalid_input: "Check your input", not_found: "Record not found",
  out_of_scope: "Outside Réseau's GitHub scope", network: "Can't reach Réseau",
};
const FINAL = new Set(["not_configured", "invalid_input", "not_found", "out_of_scope"]); // retrying won't help
const INLINE_CITATIONS = 3;

const $ = (selector) => document.querySelector(selector);

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.flat().filter((c) => c != null && c !== false));
  return node;
}

async function api(path, body) {
  let res;
  try {
    res = await fetch(path, body === undefined ? undefined : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
  } catch {
    throw { kind: "network", message: "Réseau's server isn't answering. Is python -m reseau.dashboard running?" };
  }
  const data = await res.json().catch(() => null);
  if (!res.ok || !data) throw data?.error ?? { kind: "http", message: `Réseau's server answered HTTP ${res.status}.` };
  return data;
}

// ---- formatting ----

const pad = (n) => String(n).padStart(2, "0");
const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const capitalize = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const names = (upstreams) => (upstreams?.length ? upstreams : ["a provider"]).map((u) => UPSTREAMS[u] ?? u).join(" and ");

function longDay(day) { // "2026-09-27" -> "Sunday, 27 September", read as a calendar day, not a UTC instant
  const [y, m, d] = (day || "").split("-").map(Number);
  return y ? new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" }) : "";
}

function when(timestamp) {
  const t = new Date(timestamp);
  return isNaN(t) ? timestamp : t.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function greeting(now = new Date()) {
  return now.getHours() < 12 ? "Good morning" : now.getHours() < 18 ? "Good afternoon" : "Good evening";
}

// "linear:issue:ENG-142" -> "ENG-142", "github:pr:o/r#9" -> "PR #9", "github:commit:o/r@abc…" -> "SHA abc1234"
function label(id) {
  const [source, kind] = id.split(":", 2);
  const key = id.slice(source.length + kind.length + 2);
  const pr = key.split("#")[1]?.split("/")[0];
  switch (`${source}:${kind}`) {
    case "linear:issue": return key;
    case "github:pr": return `PR #${pr}`;
    case "github:commit": return `SHA ${key.split("@")[1].slice(0, 7)}`;
    case "github:review_comment": return `PR #${pr} comment`;
    case "github:review": return `PR #${pr} review`;
    case "graph8:conversation": {
      const [channel, cid] = key.split("/");
      return `${channel === "meeting" ? "Meeting" : capitalize(channel)} ${cid.slice(0, 8)}`;
    }
    default: return source === "graph8" ? `${capitalize(kind)} ${key.slice(0, 8)}` : id;
  }
}

function safeUrl(url) { // only http(s) links out: a record's URL is upstream data
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
}

// ---- claims and citations: the one component all three surfaces share ----

function cite(id, onclick, pressed) {
  return h("button", {
    type: "button", class: "cite", "data-source": id.split(":")[0], "data-activity-id": id, title: id,
    "aria-label": `View evidence: ${label(id)}`, "aria-pressed": pressed, onclick,
  }, label(id));
}

function claim(sentence) {
  const ids = sentence.activity_ids || [];
  if (!ids.length) {
    return h("li", { class: sentence.text === NOTHING ? "claim claim-empty" : "claim" },
      h("p", { class: "claim-text" }, sentence.text));
  }
  const shown = ids.slice(0, INLINE_CITATIONS);
  const open = (id) => () => openEvidence(sentence, id);
  return h("li", {
    class: "claim cited",
    // the whole line opens its first source; the citation buttons are the keyboard route to each one
    onclick: (e) => e.target.closest("button, a") || open(ids[0])(),
  },
  h("p", { class: "claim-text" }, sentence.text),
  h("div", { class: "citations" },
    shown.map((id) => cite(id, open(id))),
    ids.length > shown.length && h("button", {
      type: "button", class: "cite cite-more", "aria-label": `View all ${ids.length} sources`, onclick: open(ids[0]),
    }, `+${ids.length - shown.length}`)));
}

const claims = (sentences) => h("ul", { class: "claims" }, (sentences || []).map(claim));
const card = (title, sentences) => h("section", { class: "card" }, h("h2", { class: "card-title" }, title),
  claims(sentences));
const summary = (sentences) => h("div", { class: "summary" }, claims(sentences));
const verified = (text) => h("p", { class: "verified" }, text);

function gaps(incomplete) {
  if (!incomplete?.length) return null;
  return h("details", { class: "note" },
    h("summary", {}, `Some data may be missing (${incomplete.length})`),
    h("ul", {}, incomplete.map((g) => h("li", {}, `${UPSTREAMS[g.source] ?? g.source} ${g.tool}: ${g.detail}`))));
}

// ---- the evidence sheet ----

const sheet = () => $("#evidence");
const records = new Map(); // activity_id -> record; a record doesn't change while the page is open
let selected = null;

function openEvidence(sentence, id) {
  $("#evidence-claim").textContent = sentence.text;
  const row = $("#evidence-citations");
  row.hidden = sentence.activity_ids.length < 2;
  row.replaceChildren(...sentence.activity_ids.map((i) => cite(i, () => select(i), "false")));
  if (!sheet().open) sheet().showModal();
  select(id);
}

function select(id) {
  selected = id;
  for (const b of $("#evidence-citations").children) b.setAttribute("aria-pressed", String(b.dataset.activityId === id));
  showRecord(id);
}

async function showRecord(id) {
  const box = $("#evidence-record");
  box.setAttribute("aria-busy", "true");
  box.replaceChildren(h("div", { class: "loading", role: "status" },
    h("div", { class: "spinner", "aria-hidden": "true" }), h("p", { class: "loading-text" }, `Opening ${label(id)}…`)));
  let content;
  try {
    const record = records.get(id) ?? await api(`/api/evidence?activity_id=${encodeURIComponent(id)}`);
    records.set(id, record);
    content = recordCard(record);
  } catch (err) {
    content = failure(err, () => showRecord(id));
  }
  if (selected !== id) return; // another source was picked meanwhile
  box.removeAttribute("aria-busy");
  box.replaceChildren(content);
}

function recordCard(r) {
  const kind = `${r.source}:${r.kind}`;
  const actor = r.actor || {};
  const who = actor.person ? capitalize(actor.person) : actor.name || actor.id;
  const url = safeUrl(r.url);
  const repo = r.source === "github" ? r.activity_id.split(":")[2].split(/[#@]/)[0] : null;
  const row = (term, value) => value && h("div", {}, h("dt", {}, term), h("dd", {}, value));
  const time = (t) => t && h("time", { datetime: t }, when(t));
  return h("article", { class: "record-card", "data-source": r.source },
    h("p", { class: "record-kind" }, KINDS[kind] ?? kind),
    h("h3", { class: "record-title" }, r.title || label(r.activity_id)),
    h("p", { class: "record-key" }, [label(r.activity_id), repo].filter(Boolean).join(" · ")),
    h("dl", { class: "record-meta" },
      row(r.source === "graph8" ? "Owner" : "By", who && [who, actor.identity !== "mapped" &&
        h("span", { class: "badge", title: "Not in Réseau's identity map" }, "unmapped")]),
      row("Created", time(r.created_at)), row("Updated", time(r.updated_at)), row("Fetched", time(r.fetched_at))),
    url
      ? h("a", { class: "link-out", href: url, target: "_blank", rel: "noopener noreferrer" },
        `Open in ${UPSTREAMS[r.source] ?? r.source}`)
      : h("p", { class: "no-link" }, "Graph8 records have no web address. Cite this one by its ID:"),
    h("code", { class: "activity-id" }, r.activity_id));
}

// ---- states: loading, error ----

function loading(text) {
  const elapsed = h("span", { class: "elapsed" }, "0 s");
  const node = h("div", { class: "loading", role: "status" },
    h("div", { class: "spinner", "aria-hidden": "true" }),
    h("p", { class: "loading-text" }, `${text}… `, elapsed),
    h("p", { class: "loading-hint" }, "A Graph8 run takes about half a minute. Every sentence is checked before it's shown."),
    h("div", { class: "skeleton", "aria-hidden": "true" }, h("span"), h("span"), h("span")));
  const started = Date.now();
  node.timer = setInterval(() => { elapsed.textContent = `${Math.round((Date.now() - started) / 1000)} s`; }, 1000);
  return node;
}

function failure(err, retry) {
  const title = err.kind === "upstream_unavailable" ? `${capitalize(names(err.upstreams))} ${err.upstreams?.length > 1 ? "are" : "is"} unavailable`
    : err.kind === "upstream_error" ? `${capitalize(names(err.upstreams))} returned an error`
      : ERRORS[err.kind] ?? "Something went wrong";
  return h("div", { class: `state state-error${err.kind === "upstream_unavailable" ? " state-unavailable" : ""}`, role: "alert" },
    h("h2", { class: "state-title" }, title),
    h("p", { class: "state-text" }, err.message),
    err.problems?.length && h("details", { class: "problems" },
      h("summary", {}, `What failed (${err.problems.length})`), h("ul", {}, err.problems.map((p) => h("li", {}, p)))),
    !FINAL.has(err.kind) && h("button", { type: "button", class: "secondary", onclick: retry }, "Try again"));
}

async function run(surface, text, path, body, render) {
  const box = $(`#${surface}-result`);
  const controls = document.querySelectorAll(`#${surface} button, #${surface} input`);
  controls.forEach((c) => { c.disabled = true; });
  const spinner = loading(text);
  box.setAttribute("aria-busy", "true");
  box.replaceChildren(spinner);
  try {
    box.replaceChildren(...render(await api(path, body)).filter(Boolean));
  } catch (err) {
    box.replaceChildren(failure(err, () => run(surface, text, path, body, render)));
  } finally {
    clearInterval(spinner.timer);
    box.removeAttribute("aria-busy");
    controls.forEach((c) => { c.disabled = false; });
  }
}

// ---- the three surfaces ----

function briefing(data) {
  $("#today-title").textContent = `${greeting()}${data.me?.person ? `, ${capitalize(data.me.person)}` : ""}.`;
  $("#today-date").textContent = longDay(data.date);
  return [
    summary(data.sections.summary),
    h("div", { class: "cards" }, SECTIONS.today.map(([key, title]) => card(title, data.sections[key]))),
    verified("Every sentence was checked against the GitHub and Linear records it cites."),
    gaps(data.incomplete)];
}

function report(data) {
  return [
    h("p", { class: "result-meta" }, `${data.team} · ${longDay(data.date)}`),
    summary(data.sections.summary),
    h("div", { class: "cards" }, SECTIONS.report.map(([key, title]) => card(title, data.sections[key]))),
    verified("Every number matches its source, and every line cites it."),
    data.unmapped?.length && h("p", { class: "note" }, `Not counted, not in the identity map: ${
      data.unmapped.map((a) => a.name || a.id).join(", ")}.`),
    gaps(data.incomplete)];
}

function answer(data) {
  const sentences = data.sections.answer;
  const declined = sentences.every((s) => !s.activity_ids?.length);
  return [
    h("p", { class: "question" }, data.question),
    declined
      ? h("div", { class: "state state-empty" }, h("h2", { class: "state-title" }, "No evidence found."),
        h("p", { class: "state-text" }, data.tool ? `${data.tool}'s output doesn't answer this question.`
          : "None of Réseau's tools covers this question, so it wasn't answered."))
      : h("div", { class: "card answer" }, claims(sentences)),
    data.tool && h("p", { class: "result-meta" }, "Answered from ",
      h("code", {}, `${data.tool}(${Object.values(data.arguments || {}).join(", ")})`)),
    !declined && verified("Every sentence cites the records it came from."),
    gaps(data.incomplete)];
}

function empty(text) {
  return h("div", { class: "state state-empty" }, h("p", { class: "state-text" }, text));
}

// ---- wiring ----

function route(moveFocus) {
  const current = SURFACES.includes(location.hash.slice(1)) ? location.hash.slice(1) : "today";
  for (const s of SURFACES) $(`#${s}`).hidden = s !== current;
  for (const a of document.querySelectorAll(".segmented a")) {
    if (a.hash === `#${current}`) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  if (moveFocus) {
    window.scrollTo(0, 0);
    $(`#${current}-title`).focus();
  }
}

function init() {
  const today = new Date();
  $("#today-title").textContent = `${greeting(today)}.`;
  $("#today-date").textContent = longDay(isoDay(today));
  $("#today-result").replaceChildren(empty("Start My Day runs on Graph8 and takes about half a minute."));
  $("#start").addEventListener("click", () => run("today", "Graph8 is writing your briefing", "/api/start-my-day", {}, briefing));

  const day = $("#report-date");
  day.max = isoDay(today);
  day.value = isoDay(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1));
  $("#report-result").replaceChildren(empty("Pick a day and run the report."));
  $("#report-form").addEventListener("submit", (e) => {
    e.preventDefault();
    run("report", "Graph8 is writing the report", "/api/daily-report", { date: day.value }, report);
  });

  const question = $("#ask-input");
  $("#ask-form").addEventListener("submit", (e) => {
    e.preventDefault();
    run("ask", "Graph8 is finding the answer", "/api/ask", { question: question.value.trim() }, answer);
  });
  for (const s of document.querySelectorAll(".suggestion")) {
    s.addEventListener("click", () => { question.value = s.textContent; $("#ask-form").requestSubmit(); });
  }

  const dialog = sheet();
  $("#evidence-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (e) => { // a click on the backdrop closes; keyboard clicks land on buttons
    const r = dialog.getBoundingClientRect();
    if (e.target === dialog && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)) {
      dialog.close();
    }
  });

  addEventListener("hashchange", () => route(true));
  route(false);
}

init();
