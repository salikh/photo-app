"""Measure how fast the viewer moves to the next photo and zooms, on large photos.

  .venv/bin/python tools/measure_navigation.py [--photos=8] [--json]

Serves a synthetic library of 16-megapixel JPEGs (4928x3264, the size of the real camera files) from a
temporary directory and drives Chrome. Reports, in milliseconds from the key press until the image is
loaded (naturalWidth > 0) and painted (next animation frame):

  next      ArrowRight after the preloads had time to settle
  next2     the photo two steps ahead
  prev2     two steps back
  zoom      `Z` (the full-size image)
  fast      four ArrowRight in quick succession, time until the last photo is shown
  back2     from the last photo, two ArrowLeft in quick succession: time until the photo two back
            is shown (not preloaded by a window of one photo back)
"""

import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.e2e import harness  # noqa: E402

SIZE = (4928, 3264)


def big_library(root, count=8):
  import random
  rnd = random.Random(1)
  for i in range(count):
    img = Image.effect_noise(SIZE, 40).convert("RGB")
    grad = Image.linear_gradient("L").resize(SIZE).rotate(90 * (i % 4))
    img = Image.merge("RGB", (grad, img.split()[1], Image.new("L", SIZE, rnd.randrange(256))))
    path = os.path.join(root, "trip", f"IMG_{i + 1:04d}.jpg")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, quality=88)


TIMER = """
async (args) => {
  const [key, selector, want] = args;
  const img = document.querySelector('.stage img.main');
  const ready = () => {
    const i = document.querySelector('.stage img.main');
    return i && i.complete && i.naturalWidth >= want && document.querySelector('.hud .name').textContent.includes(selector);
  };
  return await new Promise((resolve) => {
    const t0 = performance.now();
    const check = () => {
      if (ready()) requestAnimationFrame(() => requestAnimationFrame(() => resolve(performance.now() - t0)));
      else setTimeout(check, 4);
    };
    window.__go(key);
    check();
  });
}
"""


def main():
  count = 8
  for a in sys.argv[1:]:
    if a.startswith("--photos="):
      count = int(a.split("=")[1])
  harness.build_library = lambda root: big_library(root, count)
  tmp = pathlib.Path(tempfile.mkdtemp())
  srv = harness.Server(tmp).start()
  results = {}
  try:
    with sync_playwright() as p:
      b = p.chromium.launch(channel="chrome", headless=True)
      page = b.new_page(viewport={"width": 1280, "height": 800})
      ids = [x["id"] for x in json.load(__import__("urllib.request").request.urlopen(srv.url + "/api/photos?dir=trip&sort=name"))["photos"]]
      page.goto(f"{srv.url}/#/trip?photo={ids[0]}")
      page.wait_for_selector(".loupe:not([hidden])")
      page.evaluate("window.__go = (k) => document.dispatchEvent(new KeyboardEvent('keydown', {key: k, bubbles: true}))")
      page.wait_for_timeout(4000)                                   # let the preloads settle
      medium = 2000
      def timed(key, name, want):
        return page.evaluate(TIMER, [key, name, want])
      results["next"] = timed("ArrowRight", "IMG_0002", medium - 1)
      page.wait_for_timeout(3000)
      results["next2"] = timed("ArrowRight", "IMG_0003", medium - 1)
      page.wait_for_timeout(3000)
      results["prev2"] = timed("ArrowLeft", "IMG_0002", medium - 1)
      page.wait_for_timeout(200)
      results["prev2_second_step"] = timed("ArrowLeft", "IMG_0001", medium - 1)
      page.wait_for_timeout(3000)
      results["zoom"] = timed("z", "IMG_0001", SIZE[0] - 1)
      page.evaluate("window.__go('z')")                             # back to fit
      page.wait_for_timeout(2500)
      t = page.evaluate("""async () => {
        const t0 = performance.now();
        for (let i = 0; i < 4; i++) { window.__go('ArrowRight'); await new Promise(r => setTimeout(r, 30)); }
        return await new Promise((resolve) => {
          const check = () => {
            const i = document.querySelector('.stage img.main');
            if (i.complete && i.naturalWidth >= 1999 && document.querySelector('.hud .name').textContent.includes('IMG_0005'))
              requestAnimationFrame(() => requestAnimationFrame(() => resolve(performance.now() - t0)));
            else setTimeout(check, 4);
          };
          check();
        });
      }""")
      results["fast"] = t
      # a cold jump on a fresh page: open the first photo, jump to the last, then two quick steps back
      page2 = b.new_page(viewport={"width": 1280, "height": 800})
      page2.goto(f"{srv.url}/#/trip?photo={ids[0]}")
      page2.wait_for_selector(".loupe:not([hidden])")
      page2.evaluate("window.__go = (k) => document.dispatchEvent(new KeyboardEvent('keydown', {key: k, bubbles: true}))")
      page2.wait_for_timeout(2500)
      page2.evaluate("window.__go('End')")
      page2.wait_for_timeout(4000)
      target = f"IMG_{count - 2:04d}"
      results["back2"] = page2.evaluate("""async (target) => {
        const t0 = performance.now();
        window.__go('ArrowLeft');
        await new Promise(r => setTimeout(r, 30));
        window.__go('ArrowLeft');
        return await new Promise((resolve) => {
          const check = () => {
            const i = document.querySelector('.stage img.main');
            if (i.complete && i.naturalWidth >= 1999 && document.querySelector('.hud .name').textContent.includes(target))
              requestAnimationFrame(() => requestAnimationFrame(() => resolve(performance.now() - t0)));
            else setTimeout(check, 4);
          };
          check();
        });
      }""", target)
      b.close()
  finally:
    srv.stop()
  results = {k: round(v) for k, v in results.items()}
  print(json.dumps(results) if "--json" in sys.argv else "\n".join(f"{k:18s}{v:6d} ms" for k, v in results.items()))


if __name__ == "__main__":
  main()
