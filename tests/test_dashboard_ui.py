"""HAR-106: the dashboard page in a real browser, Playwright driving the installed Chrome (or Playwright's Chromium,
after `uv run playwright install chromium`). The component tests answer /api/* with fixture responses, the real
backend's output for the fixture world (tests/fixture_dashboard.py), so each state is set up exactly. The one E2E
test runs the critical path against the fixture-backed backend itself."""
import json
import re
from datetime import date, timedelta
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen

import pytest
from playwright.sync_api import Error, expect, sync_playwright

from reseau import semantic, workflows
from tests import fixture_dashboard as fd
from tests.test_semantic import NOW, UI, YESTERDAY

PR_9 = "github:pr:%s#9" % UI
NOTHING = {s: [{"text": "Nothing to report.", "activity_ids": []}] for s in workflows.SECTIONS}


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome")
        except Error:
            b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="module")
def site():
    """The fixture-backed backend. Component tests intercept its /api/*; the E2E test doesn't."""
    with pytest.MonkeyPatch.context() as m:
        m.setattr(semantic, "now", lambda: NOW)
        with fd.serving(fd.app()) as url:
            yield url


@pytest.fixture(scope="module")
def fixtures(site):
    """Each surface's response from the fixture-backed backend, and the evidence record of every activity_id cited."""
    def post(api, body):
        return json.load(urlopen(Request(site + "/api/" + api, json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})))

    out = {"start-my-day": post("start-my-day", {}), "daily-report": post("daily-report", {"date": YESTERDAY}),
           "ask": post("ask", {"question": "Why does ENG-142 matter?"})}
    cited = {i for o in out.values() for section in o["sections"].values() for s in section for i in s["activity_ids"]}
    out["evidence"] = {i: json.load(urlopen(site + "/api/evidence?" + urlencode({"activity_id": i}))) for i in cited}
    return out


@pytest.fixture
def page(browser, site):
    context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
    page = context.new_page()
    page.console = []
    page.on("console", lambda m: page.console.append(m))  # Playwright takes no builtins as handlers
    page.on("pageerror", lambda e: page.console.append(e))
    yield page
    context.close()


def answer(page, api, body=None, status=200, sent=None):
    """Answer /api/<api> with body. sent collects the JSON bodies the page posted."""
    def handle(route):
        if sent is not None:
            sent.append(route.request.post_data_json)
        route.fulfill(status=status, json=body)
    page.route(re.compile(r"/api/%s(\?|$)" % api), handle)


def records(page, fixtures, fail=None):
    """Answer /api/evidence from the fixture records; fail = (status, error) answers every one with that error."""
    def handle(route):
        body = {"error": fail[1]} if fail else fixtures["evidence"][unquote(route.request.url.split("activity_id=")[1])]
        route.fulfill(status=fail[0] if fail else 200, json=body)
    page.route(re.compile(r"/api/evidence\?"), handle)


def start(page, site, fixtures, briefing=None):
    answer(page, "start-my-day", briefing or fixtures["start-my-day"])
    records(page, fixtures)
    page.goto(site)
    page.get_by_role("button", name="Start My Day").click()
    expect(page.get_by_role("heading", name="Focus today")).to_be_visible()


def card(page, title):
    return page.locator(".card", has=page.get_by_role("heading", name=title))


# ---- rendering ----

def test_start_my_day_renders_the_briefing_sections(page, site, fixtures):
    start(page, site, fixtures)
    briefing = fixtures["start-my-day"]["sections"]
    expect(page.locator("#today-title")).to_have_text(re.compile(r"^Good (morning|afternoon|evening), Dev\.$"))
    expect(page.locator("#today-date")).to_have_text("Sunday, September 27")
    expect(page.locator(".summary")).to_contain_text(briefing["summary"][0]["text"])
    for key, title in [("focus", "Focus today"), ("needs_attention", "Needs attention"), ("yesterday", "Yesterday")]:
        expect(card(page, title).locator(".claim-text")).to_have_text([s["text"] for s in briefing[key]])
    assert page.console == []  # nothing logged: briefings carry customer and deal data


def test_red_clicking_a_citation_opens_the_evidence_panel_with_the_source_link(page, site, fixtures):
    start(page, site, fixtures)
    sheet = page.get_by_role("dialog", name="Evidence")
    expect(sheet).to_be_hidden()
    card(page, "Focus today").get_by_role("button", name="View evidence: PR #9").click()
    expect(sheet).to_be_visible()
    link = sheet.get_by_role("link", name="Open in GitHub")
    expect(link).to_have_attribute("href", "https://github.com/%s/pull/9" % UI)
    expect(link).to_have_attribute("rel", "noopener noreferrer")
    expect(sheet.locator(".record-title")).to_have_text(fixtures["evidence"][PR_9]["title"])
    expect(sheet.locator(".record-key")).to_have_text("PR #9 · %s" % UI)


def test_every_rendered_claim_links_to_its_evidence(page, site, fixtures):
    answer(page, "daily-report", fixtures["daily-report"])
    answer(page, "ask", fixtures["ask"])
    start(page, site, fixtures)
    page.goto(site + "#report")
    page.get_by_role("button", name="Run report").click()
    expect(page.get_by_role("heading", name="Blocked")).to_be_visible()
    page.goto(site + "#ask")
    page.get_by_label("Question", exact=True).fill("Why does ENG-142 matter?")
    page.get_by_role("button", name="Ask", exact=True).click()
    expect(page.locator("#ask-result .claim")).to_have_count(1)
    for surface in ("today", "report", "ask"):
        page.goto(site + "#" + surface)
        claims = page.locator("#%s-result .claim:not(.claim-empty)" % surface)
        assert claims.count() > 0
        for k in range(claims.count()):
            expect(claims.nth(k).locator("button.cite").first).to_be_visible()


def test_a_claim_line_opens_all_its_sources(page, site, fixtures):
    start(page, site, fixtures)
    focus = fixtures["start-my-day"]["sections"]["focus"][0]
    card(page, "Focus today").locator(".claim-text").first.click()
    sheet = page.get_by_role("dialog", name="Evidence")
    expect(sheet.locator(".sheet-claim")).to_have_text(focus["text"])
    chips = sheet.locator("#evidence-citations button.cite")
    expect(chips).to_have_count(len(focus["activity_ids"]))
    expect(chips.first).to_have_attribute("aria-pressed", "true")
    expect(sheet.locator(".record-title")).to_have_text(fixtures["evidence"][focus["activity_ids"][0]]["title"])
    sheet.get_by_role("button", name="View evidence: PR #9").click()
    expect(sheet.get_by_role("button", name="View evidence: PR #9")).to_have_attribute("aria-pressed", "true")
    expect(sheet.get_by_role("link", name="Open in GitHub")).to_have_attribute("href", "https://github.com/%s/pull/9" % UI)


def test_many_sources_fold_into_a_more_button(page, site, fixtures):
    start(page, site, fixtures)
    yesterday = fixtures["start-my-day"]["sections"]["yesterday"][0]["activity_ids"]
    more = card(page, "Yesterday").get_by_role("button", name="View all %d sources" % len(yesterday))
    expect(more).to_have_text("+%d" % (len(yesterday) - 3))
    more.click()
    expect(page.locator("#evidence-citations button.cite")).to_have_count(len(yesterday))
    expect(page.get_by_role("dialog").get_by_role("link", name="Open in GitHub")).to_have_attribute(
        "href", re.compile(r"^https://github\.com/.+/commit/[0-9a-f]{40}$"))


def test_a_graph8_record_is_cited_by_its_id(page, site, fixtures):
    answer(page, "ask", fixtures["ask"])
    records(page, fixtures)
    page.goto(site + "#ask")
    page.get_by_label("Question", exact=True).fill("Why does ENG-142 matter?")
    page.keyboard.press("Enter")
    expect(page.locator(".result-meta")).to_contain_text("Answered from get_business_context(linear:issue:ENG-142)")
    customer = next(i for i in fixtures["ask"]["sections"]["answer"][0]["activity_ids"] if i.startswith("graph8:customer"))
    page.locator("#ask-result").get_by_role("button", name=re.compile("^View all")).click()
    page.locator("#evidence-citations").get_by_role("button", name=re.compile("^View evidence: Customer")).click()
    sheet = page.get_by_role("dialog")
    expect(sheet.locator(".record-kind")).to_have_text("Graph8 customer")
    expect(sheet.get_by_role("link")).to_have_count(0)
    expect(sheet.locator(".activity-id")).to_have_text(customer)


# ---- loading, empty and error states ----

def test_loading_state_while_graph8_runs(page, site, fixtures):
    held = []
    page.route("**/api/start-my-day", lambda route: held.append(route))
    page.goto(site)
    button = page.get_by_role("button", name="Start My Day")
    button.click()
    status = page.locator("#today-result").get_by_role("status")
    expect(status).to_contain_text("Graph8 is writing your briefing")
    expect(page.locator("#today-result")).to_have_attribute("aria-busy", "true")
    expect(button).to_be_disabled()
    held[0].fulfill(json=fixtures["start-my-day"])
    expect(page.get_by_role("heading", name="Focus today")).to_be_visible()
    expect(button).to_be_enabled()
    expect(page.locator("#today-result")).not_to_have_attribute("aria-busy", "true")


def test_empty_states(page, site, fixtures):
    page.goto(site)
    expect(page.locator("#today-result")).to_contain_text("Start My Day runs on Graph8")
    page.goto(site + "#report")
    expect(page.locator("#report-result")).to_contain_text("Pick a day and run the report.")
    page.goto(site)
    start(page, site, fixtures, dict(fixtures["start-my-day"], sections=NOTHING))
    expect(page.locator("#today-result .claim-empty")).to_have_count(len(NOTHING))
    expect(page.locator("#today-result button.cite")).to_have_count(0)
    answer(page, "ask", {"question": "Will it rain?", "tool": None, "arguments": {}, "execution_ids": {"route": "ex-1"},
                         "sections": {"answer": [{"text": "No evidence found.", "activity_ids": []}]}, "incomplete": []})
    page.goto(site + "#ask")
    page.get_by_label("Question", exact=True).fill("Will it rain?")
    page.keyboard.press("Enter")
    expect(page.locator("#ask-result .state-empty")).to_contain_text("No evidence found.")
    expect(page.locator("#ask-result")).to_contain_text("None of Réseau's tools covers this question")


UNAVAILABLE = (503, {"kind": "upstream_unavailable", "upstreams": ["graph8"],
                     "message": "Graph8 is unavailable right now, so the workflow couldn't run. Try again in a minute."})
UNVERIFIED = (502, {"kind": "unverified", "message": "Graph8's reply failed Réseau's checks on every attempt.",
                    "problems": ["focus[0]: cites 'linear:issue:HAR-999', which the tool did not return for focus"]})


@pytest.mark.parametrize("surface, api, submit", [
    ("today", "start-my-day", lambda p: p.get_by_role("button", name="Start My Day").click()),
    ("report", "daily-report", lambda p: p.get_by_role("button", name="Run report").click()),
    ("ask", "ask", lambda p: (p.get_by_label("Question", exact=True).fill("What is blocked?"), p.keyboard.press("Enter"))),
])
def test_error_states(page, site, fixtures, surface, api, submit):
    page.goto(site + "#" + surface)
    result = page.locator("#%s-result" % surface)
    answer(page, api, {"error": UNAVAILABLE[1]}, UNAVAILABLE[0])
    submit(page)
    alert = result.get_by_role("alert")
    expect(alert.get_by_role("heading")).to_have_text("Graph8 is unavailable")
    expect(alert).to_contain_text("Try again in a minute")
    page.unroute(re.compile(r"/api/%s(\?|$)" % api))
    answer(page, api, {"error": UNVERIFIED[1]}, UNVERIFIED[0])
    alert.get_by_role("button", name="Try again").click()
    expect(alert.get_by_role("heading")).to_have_text("Graph8's reply didn't pass verification")
    alert.locator("summary").click()
    expect(alert.locator("details li")).to_have_text(UNVERIFIED[1]["problems"])
    page.unroute(re.compile(r"/api/%s(\?|$)" % api))
    page.route(re.compile(r"/api/%s(\?|$)" % api), lambda route: route.abort())
    alert.get_by_role("button", name="Try again").click()
    expect(alert.get_by_role("heading")).to_have_text("Can't reach Réseau")
    page.unroute(re.compile(r"/api/%s(\?|$)" % api))
    answer(page, api, {"error": {"kind": "not_configured", "message": "RESEAU_ASK is not set."}}, 503)
    alert.get_by_role("button", name="Try again").click()
    expect(alert.get_by_role("heading")).to_have_text("Not set up yet")
    expect(alert.get_by_role("button", name="Try again")).to_have_count(0)  # retrying can't fix it


def test_evidence_error_when_its_provider_is_down(page, site, fixtures):
    start(page, site, fixtures)
    page.unroute(re.compile(r"/api/evidence\?"))
    records(page, fixtures, fail=(503, {"kind": "upstream_unavailable", "upstreams": ["linear"],
                                        "message": "Linear is unavailable right now, so this record can't be opened."}))
    card(page, "Focus today").get_by_role("button", name="View evidence: HAR-7").click()
    alert = page.get_by_role("dialog").get_by_role("alert")
    expect(alert.get_by_role("heading")).to_have_text("Linear is unavailable")
    page.unroute(re.compile(r"/api/evidence\?"))
    records(page, fixtures)
    alert.get_by_role("button", name="Try again").click()
    expect(page.get_by_role("dialog").get_by_role("link", name="Open in Linear")).to_have_attribute(
        "href", "https://linear.app/acme/issue/HAR-7")


# ---- the report and Ask Réseau send what was picked and typed ----

def test_report_posts_the_picked_day(page, site, fixtures):
    sent = []
    answer(page, "daily-report", fixtures["daily-report"], sent=sent)
    page.goto(site + "#report")
    day = page.get_by_label("Day")
    expect(day).to_have_value((date.today() - timedelta(days=1)).isoformat())  # the browser's own yesterday
    day.fill("2026-09-25")
    page.get_by_role("button", name="Run report").click()
    expect(page.get_by_role("heading", name="Completed")).to_be_visible()
    assert sent == [{"date": "2026-09-25"}]
    for title in ("Completed", "Merged", "Commits", "Blocked"):
        expect(card(page, title)).to_be_visible()


def test_ask_posts_the_question(page, site, fixtures):
    sent = []
    answer(page, "ask", fixtures["ask"], sent=sent)
    page.goto(site + "#ask")
    page.get_by_role("button", name="Why does HAR-104 matter?").click()
    expect(page.locator("#ask-result .claim")).to_have_count(1)
    assert sent == [{"question": "Why does HAR-104 matter?"}]
    expect(page.get_by_label("Question", exact=True)).to_have_value("Why does HAR-104 matter?")


# ---- accessibility and layout ----

def test_keyboard_only(page, site, fixtures):
    answer(page, "start-my-day", fixtures["start-my-day"])
    records(page, fixtures)
    page.goto(site)
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Skip to content")).to_be_focused()
    page.get_by_role("link", name="Report").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#report-title")).to_be_focused()
    expect(page.get_by_role("link", name="Report")).to_have_attribute("aria-current", "page")
    page.get_by_role("link", name="Today").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#today-title")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.get_by_role("button", name="Start My Day")).to_be_focused()
    page.keyboard.press("Enter")
    chip = card(page, "Focus today").get_by_role("button", name="View evidence: HAR-7")
    chip.focus()
    page.keyboard.press("Enter")
    sheet = page.get_by_role("dialog", name="Evidence")
    expect(sheet.get_by_role("link", name="Open in Linear")).to_be_visible()
    page.keyboard.press("Escape")
    expect(sheet).to_be_hidden()
    expect(chip).to_be_focused()  # focus returns to the citation that opened it


def test_controls_have_accessible_names(page, site):
    page.goto(site)
    unnamed = page.evaluate("""() => [...document.querySelectorAll('input, button, a')].filter(e =>
        !(e.labels?.length || e.getAttribute('aria-label') || e.textContent.trim())).map(e => e.outerHTML)""")
    assert unnamed == []


@pytest.mark.parametrize("width", [360, 390, 768, 1440])
def test_no_horizontal_scroll(browser, site, fixtures, width):
    context = browser.new_context(viewport={"width": width, "height": 800}, locale="en-US")
    page = context.new_page()
    start(page, site, fixtures)
    card(page, "Yesterday").get_by_role("button", name=re.compile("^View all")).click()
    expect(page.get_by_role("dialog").get_by_role("link")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    context.close()


# ---- E2E: the critical path against the fixture-backed backend ----

def test_e2e_start_my_day_to_evidence(page, site):
    requests = []
    page.on("request", lambda r: requests.append(r.url))
    page.goto(site)
    page.get_by_role("button", name="Start My Day").click()
    focus = card(page, "Focus today")
    expect(focus).to_contain_text("UI Critic Phase 3", timeout=30_000)
    focus.get_by_role("button", name="View evidence: PR #9").click()
    expect(page.get_by_role("dialog").get_by_role("link", name="Open in GitHub")).to_have_attribute(
        "href", "https://github.com/%s/pull/9" % UI)
    assert requests and all(u.startswith(site + "/") for u in requests)  # the browser talks only to Réseau
    assert page.console == []
