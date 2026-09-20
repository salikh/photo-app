"""Shared harness for browser tests: a synthetic library and a live server."""

import os
import socket
import threading
import time

import uvicorn
from PIL import Image, ImageDraw

from photoapp import api
from photoapp import config
from photoapp import db
from tests.test_scan_sidecars import XMP, write


def make_picture(path, index, size=(1600, 1067)):
  """A picture that is easy to tell apart: a colored gradient with a number."""
  os.makedirs(os.path.dirname(path), exist_ok=True)
  img = Image.new("RGB", size)
  px = img.load()
  for x in range(size[0]):
    for y in range(0, size[1]):
      px[x, y] = ((index * 40 + x // 8) % 256, (y // 6 + index * 20) % 256, (index * 70) % 256)
  ImageDraw.Draw(img).text((size[0] // 2 - 20, size[1] // 2), str(index), fill="white")
  img.save(path)


def build_library(root):
  n = 0
  for folder, count in (("2024/trip", 6), ("2024/home", 3), ("2023", 2)):
    for i in range(count):
      n += 1
      size = (1067, 1600) if (folder == "2024/trip" and i == 3) else (1600, 1067)
      make_picture(os.path.join(root, folder, f"IMG_{n:04d}.jpg"), n, size)
  # one pre-rated picture and one rejected
  write(os.path.join(root, "2024/trip", "IMG_0002.jpg.xmp"), XMP % (4, ""), mtime=1_000_000)
  write(os.path.join(root, "2024/trip", "IMG_0003.jpg.xmp"), XMP % (-1, ""), mtime=1_000_000)


def free_port():
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


class Server:
  def __init__(self, tmp_path, **settings_overrides):
    self.pictures = str(tmp_path / "pics")
    os.makedirs(self.pictures)
    build_library(self.pictures)
    self.settings = config.Settings(
        pictures_dir=self.pictures, thumbs_dir=str(tmp_path / "thumbs"),
        state_dir=str(tmp_path / "state"), **settings_overrides)
    conn = db.open_state(self.settings.state_dir)
    self.app = api.create_app(conn, self.settings)
    self.app.state.jobs.start()
    self.port = free_port()
    self.url = f"http://127.0.0.1:{self.port}"
    self.server = uvicorn.Server(uvicorn.Config(
        self.app, host="127.0.0.1", port=self.port, log_level="warning"))
    self.thread = threading.Thread(target=self.server.run, daemon=True)

  def start(self):
    self.thread.start()
    for _ in range(100):
      try:
        with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
          break
      except OSError:
        time.sleep(0.05)
    self.app.state.scanner.start()
    self.app.state.scanner.wait()
    return self

  def stop(self):
    self.server.should_exit = True
    self.thread.join(5)
    self.app.state.jobs.stop()

  def sidecar_rating(self, rel):
    from photoapp import xmp
    path = os.path.join(self.pictures, rel)
    if not os.path.exists(path):
      return None
    return xmp.parse(open(path, "rb").read()).rating
