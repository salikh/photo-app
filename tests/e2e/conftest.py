import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

from tests.e2e.harness import Server  # noqa: E402


@pytest.fixture(scope="session")
def browser():
  with playwright_sync.sync_playwright() as p:
    try:
      b = p.chromium.launch(channel="chrome", headless=True)
    except Exception as e:  # no system Chrome installed
      pytest.skip(f"Chrome not available: {e}")
    yield b
    b.close()


@pytest.fixture
def server(tmp_path):
  s = Server(tmp_path).start()
  yield s
  s.stop()


@pytest.fixture
def page(browser, server):
  ctx = browser.new_context(viewport={"width": 1280, "height": 800})
  pg = ctx.new_page()
  pg.errors = []
  pg.on("pageerror", lambda e: pg.errors.append(str(e)))
  pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
  yield pg
  ctx.close()
  assert pg.errors == [], pg.errors


@pytest.fixture
def phone(browser, server):
  ctx = browser.new_context(viewport={"width": 390, "height": 800}, has_touch=True,
                            is_mobile=True, device_scale_factor=2)
  pg = ctx.new_page()
  pg.errors = []
  pg.on("pageerror", lambda e: pg.errors.append(str(e)))
  pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
  yield pg
  ctx.close()
  assert pg.errors == [], pg.errors
