# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.28", "playwright==1.64.0", "python-dotenv>=1.2"]
# ///
"""Hosted persona run (D10, Gate C): Ana, Ben and Cy through the public shop, end to end.

    uv run scripts/e2e_hosted.py                              # Ana, Ben, Cy, one after another
    uv run scripts/e2e_hosted.py --persona ben                # a subset
    uv run scripts/e2e_hosted.py --persona ana --count 5 --together   # concurrency measurement

For each order it opens a fresh browser context, asks the shopping agent "buy the blue backpack in
medium", clicks the PayPal button, logs in to the sandbox pop-up as that persona's buyer and
approves. Each order gets its own context, so no PayPal login carries over to the next buyer
(the browser equivalent of logging out at www.sandbox.paypal.com). It then checks the order
through the merchant console's routes: final status, hit count, 9/9 receipts verified, rationale
source and the `risk_agent.started` runner, and prints the time each stage took.

With --together, every order is first taken up to PayPal's final button, then all are approved at
the same moment, so their risk checks run concurrently.

Needs in .env: PERSONA_<ANA|BEN|CY>_EMAIL and _PASSWORD (the sandbox buyers), HOSTED_WEB_URL, and
the console password as HOSTED_CONSOLE_PASSWORD or in the file --console-password-file names.
Prints order IDs, statuses and timings only; never emails, passwords or addresses. On a failure it
saves a screenshot under backend/var/e2e/ (git-ignored; PayPal's pages can show the buyer).
"""

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from playwright.async_api import Browser, Page, async_playwright

REPO = Path(__file__).resolve().parent.parent
PERSONAS = json.loads((REPO / "backend/app/personas.json").read_text())
SHOTS = REPO / "backend/var/e2e"
PROMPT = "buy the blue backpack in medium"
EXPECTED_HITS = {"ana": 0, "ben": 1, "cy": 4}
FINAL = {"captured", "held", "blocked", "voided", "failed"}
BUYER_TEXT = {
    "captured": "Paid",
    "held": "Payment on hold for review",
    "blocked": "We couldn't complete",
}
DECIDE_TIMEOUT_S = 180
# PayPal's final button on its review page; the label has changed over time, so try each.
PAY_BUTTONS = ["#payment-submit-btn", "[data-testid='submit-button-initial']"]
PAY_LABELS = r"^(Complete Purchase|Pay|Pay Now|Continue|Agree & Continue|Agree and Continue)$"
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def event_cursor(at: float) -> str:
    """An event ID that sorts before every event created after `at` (event IDs are ULIDs)."""
    ms, chars = int(at * 1000), []
    for _ in range(10):
        chars.append(CROCKFORD[ms % 32])
        ms //= 32
    return "evt_" + "".join(reversed(chars)) + "0" * 16


@dataclass
class Run:
    persona: dict
    label: str
    order_id: str | None = None
    marks: dict[str, float] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    status: str | None = None
    summary: str = ""
    expect_rationale: str = "llm"
    server: dict[str, str | None] = field(default_factory=dict)

    step: str = "start"
    pages: list = field(default_factory=list)  # for a screenshot on failure

    def mark(self, stage: str) -> None:
        self.marks[stage] = time.monotonic()


class Console:
    """The merchant console's routes, with the console password (any username)."""

    def __init__(self, web: str, password: str):
        self.http = httpx.AsyncClient(base_url=web, auth=("e2e", password), timeout=30)

    async def order(self, order_id: str) -> dict:
        response = await self.http.get(f"/api/console/orders/{order_id}")
        response.raise_for_status()
        return response.json()

    async def events(self, cursor: str) -> list[dict]:
        found = []
        while True:
            response = await self.http.get("/api/console/events", params={"after": cursor})
            response.raise_for_status()
            page = response.json()["events"]
            found += page
            if len(page) < 50:
                return found
            cursor = page[-1]["event_id"]


async def wait_until_final(console: Console, order_id: str) -> dict:
    deadline = time.monotonic() + DECIDE_TIMEOUT_S
    while True:
        detail = await console.order(order_id)
        if detail["status"] in FINAL or time.monotonic() > deadline:
            return detail
        await asyncio.sleep(1)


async def shot(page: Page, run: Run, n: int = 0) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{run.label}-{int(time.time())}-{n}.png"
    try:
        await page.screenshot(path=str(path))
        run.problems.append(f"screenshot {path.relative_to(REPO)}")
    except Exception:  # the page may be gone
        pass


async def to_pay_button(browser: Browser, web: str, run: Run, creds: tuple[str, str]):
    """Shop as the persona and log in to PayPal; return (context, page, popup, pay button)."""
    context = await browser.new_context()
    page = await context.new_page()
    run.pages = [page]
    run.mark("start")
    run.step = "chat"
    await page.goto(web)
    await page.get_by_role("radio", name=run.persona["name"]).click()
    await page.get_by_label("Message the shopping agent").fill(PROMPT)
    await page.get_by_role("button", name="Send").click()
    button = page.locator("paypal-button")
    await button.wait_for(timeout=90_000)
    run.mark("paypal_button")
    run.step = "open pop-up"
    await page.wait_for_timeout(1000)  # the SDK wires the click handler after mounting
    async with page.expect_popup(timeout=30_000) as info:
        await button.click()
    popup = await info.value
    run.pages.append(popup)
    email, password = creds
    run.step = "PayPal email"
    await popup.locator("#email").fill(email, timeout=60_000)
    if await popup.locator("#btnNext").is_visible():
        await popup.locator("#btnNext").click()
    run.step = "PayPal password"
    await popup.locator("#password").fill(password, timeout=30_000)
    await popup.locator("#btnLogin").click()
    run.mark("logged_in")
    run.step = "PayPal review page"
    pay = popup.locator(", ".join(PAY_BUTTONS)).or_(popup.get_by_role("button", name=PAY_LABELS))
    await pay.first.wait_for(timeout=60_000)
    return context, page, popup, pay.first


async def approve_and_check(page: Page, popup: Page, pay, run: Run, console: Console) -> None:
    run.mark("approve")
    await pay.click()
    # Not the pop-up closing: PayPal keeps it open until the shop's onApprove returns, and that
    # waits for the decision (up to 2 minutes), so an undecided order keeps it open.
    for _ in range(60):
        if run.order_id:
            break
        await asyncio.sleep(1)
    else:
        raise RuntimeError("the shop never called /api/orders/<id>/process")
    detail = await wait_until_final(console, run.order_id)
    run.mark("decided")
    run.status = detail["status"]
    await verify(run, detail, page)


async def verify(run: Run, detail: dict, page: Page) -> None:
    persona, want = run.persona, run.persona["status"]
    check, decision = detail.get("check") or {}, detail.get("decision") or {}
    verified = sum(1 for r in detail.get("receipts", []) if r["verified"])
    hits = [f"{h['member']}/{h['field']}" for h in check.get("hits", []) if h["is_match"]]
    run.summary = (
        f"{detail['status']}, hit_count={check.get('hit_count')} {hits}, {verified}/9 verified, "
        f"rationale={decision.get('rationale_source')}"
    )
    if detail["status"] != want:
        run.problems.append(f"status {detail['status']}, want {want}")
    if check.get("hit_count") != EXPECTED_HITS[persona["key"]]:
        run.problems.append(
            f"hit_count {check.get('hit_count')}, want {EXPECTED_HITS[persona['key']]}"
        )
    if verified != 9:
        run.problems.append(f"{verified}/9 receipts verified")
    if decision.get("rationale_source") != run.expect_rationale:
        run.problems.append(f"rationale_source {decision.get('rationale_source')}")
    text = BUYER_TEXT.get(want)
    if text and detail["status"] == want:
        try:
            await page.get_by_role("status").filter(has_text=text).wait_for(timeout=20_000)
        except Exception:
            shown = await page.get_by_role("status").inner_text()
            run.problems.append(f"shop shows {shown!r}, want {text!r}")


def check_runner(run: Run, events: list[dict], expect: str) -> None:
    mine = [e for e in events if e["order_id"] == run.order_id]
    started = [e["payload"] for e in mine if e["event_type"] == "risk_agent.started"]
    run.summary += ", runner=" + "+".join(
        p.get("runner", "?") + (f"({p['reason']})" if p.get("reason") else "") for p in started
    )
    if expect != "any" and not any(p.get("runner") == expect for p in started):
        run.problems.append(f"no risk_agent.started with runner {expect}")
    stamps = {e["event_type"]: e["created_at"] for e in mine}
    run.server = {
        k: stamps.get(k) for k in ("risk_agent.started", "check.completed", "decision.made")
    }


def stage_times(run: Run) -> str:
    m = run.marks
    pairs = [
        ("chat->button", "start", "paypal_button"),
        ("button->login", "paypal_button", "logged_in"),
        ("approve->decided", "approve", "decided"),
    ]
    return ", ".join(f"{name} {m[b] - m[a]:.1f}s" for name, a, b in pairs if a in m and b in m)


def server_times(run: Run) -> str:
    """Run start -> check done and -> decision, from the events' timestamps."""
    t = {k: datetime.fromisoformat(v.replace("Z", "+00:00")) for k, v in run.server.items() if v}
    start = t.get("risk_agent.started")
    pairs = [("started->checked", "check.completed"), ("started->decided", "decision.made")]
    return ", ".join(
        f"{name} {(t[end] - start).total_seconds():.1f}s"
        for name, end in pairs
        if start and end in t
    )


def credentials(persona: dict) -> tuple[str, str]:
    email_env = persona["email_env"]
    password_env = email_env.replace("_EMAIL", "_PASSWORD")
    email, password = os.environ.get(email_env), os.environ.get(password_env)
    if not email or not password:
        raise SystemExit(f"Set {email_env} and {password_env} in .env.")
    return email, password


async def prepare(browser: Browser, web: str, run: Run):
    try:
        context, page, popup, pay = await to_pay_button(browser, web, run, credentials(run.persona))
    except Exception as exc:
        run.problems.append(f"at {run.step}: {type(exc).__name__}: {str(exc).splitlines()[0]}")
        for n, page in enumerate(run.pages):
            await shot(page, run, n)
        if len(run.pages) > 1:  # what PayPal offered instead, to fix PAY_LABELS if it changed
            try:
                labels = await run.pages[-1].get_by_role("button").all_inner_texts()
                run.problems.append(f"pop-up buttons: {[x.strip() for x in labels if x.strip()]}")
            except Exception:
                pass
        return None

    def on_request(request):
        # onApprove posts to /api/orders/<order_id>/process: that names our order.
        path = request.url.split("?")[0]
        if request.method == "POST" and path.endswith("/process") and "/api/orders/" in path:
            run.order_id = path.split("/api/orders/")[1].split("/")[0]

    page.on("request", on_request)
    return context, page, popup, pay


async def finish(prepared, run: Run, console: Console) -> None:
    context, page, popup, pay = prepared
    try:
        await approve_and_check(page, popup, pay, run, console)
    except Exception as exc:
        run.problems.append(f"after approval: {type(exc).__name__}: {str(exc).splitlines()[0]}")
        await shot(page, run)
    finally:
        await context.close()


async def main_async(args) -> int:
    web = args.web.rstrip("/")
    console = Console(web, console_password(args))
    keys = args.persona or [p["key"] for p in PERSONAS]
    runs = []
    for key in keys:
        persona = next(p for p in PERSONAS if p["key"] == key)
        for n in range(args.count):
            label = f"{key}{n + 1}" if args.count > 1 else key
            runs.append(Run(persona, label, expect_rationale=args.expect_rationale))
    cursor = event_cursor(time.time() - 5)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        if args.together:
            prepared = await asyncio.gather(*(prepare(browser, web, r) for r in runs))
            ready = sum(1 for x in prepared if x)
            print(f"{ready} of {len(runs)} at PayPal's final button; approving them together")
            await asyncio.gather(
                *(finish(x, r, console) for x, r in zip(prepared, runs, strict=True) if x)
            )
        else:
            for run in runs:
                prepared = await prepare(browser, web, run)
                if prepared:
                    await finish(prepared, run, console)
                print(
                    f"{run.label}: {run.order_id or '-'} {run.status or 'not decided'}", flush=True
                )
        await browser.close()
    events = []
    for _ in range(3):  # the web can lose the backend for a minute after a redeploy
        try:
            events = await console.events(cursor)
            break
        except httpx.HTTPError as exc:
            print(f"reading events failed ({type(exc).__name__}); retrying", file=sys.stderr)
            await asyncio.sleep(20)
    failed = 0
    for run in runs:
        if run.order_id:
            check_runner(run, events, args.expect_runner)
        failed += bool(run.problems or not run.order_id)
        report(run)
    print(
        f"\n{len(runs) - failed} of {len(runs)} passed." if failed else f"\nAll {len(runs)} passed."
    )
    return 1 if failed else 0


def report(run: Run) -> None:
    ok = "ok  " if not run.problems and run.order_id else "FAIL"
    print(f"\n{ok} {run.persona['name']} ({run.label}) {run.order_id or '(no order)'}")
    if run.summary:
        print(f"     {run.summary}")
    times = ", ".join(x for x in (stage_times(run), server_times(run)) if x)
    if times:
        print(f"     {times}")
    for problem in run.problems:
        print(f"     ! {problem}")


def console_password(args) -> str:
    if os.environ.get("HOSTED_CONSOLE_PASSWORD"):
        return os.environ["HOSTED_CONSOLE_PASSWORD"]
    path = args.console_password_file
    if path and Path(path).is_file():
        return Path(path).read_text().strip()
    raise SystemExit("Set HOSTED_CONSOLE_PASSWORD in .env, or pass --console-password-file.")


def main() -> int:
    load_dotenv(REPO / ".env")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--persona", action="append", choices=[p["key"] for p in PERSONAS])
    parser.add_argument("--count", type=int, default=1, help="orders per persona (default 1)")
    parser.add_argument("--together", action="store_true", help="approve all orders at once")
    parser.add_argument("--web", default=os.environ.get("HOSTED_WEB_URL"))
    parser.add_argument(
        "--console-password-file", default=os.environ.get("HOSTED_CONSOLE_PASSWORD_FILE")
    )
    parser.add_argument("--expect-runner", default="render_workflow", help="or in_process, or any")
    parser.add_argument("--expect-rationale", default="llm", help="or template")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    args = parser.parse_args()
    if not args.web:
        parser.error("set HOSTED_WEB_URL in .env or pass --web")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
