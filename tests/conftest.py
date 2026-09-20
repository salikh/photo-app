import os

import pytest
from PIL import Image

from photoapp import config
from photoapp import db


def make_jpeg(path, size=(30, 20), color="red"):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  Image.new("RGB", size, color).save(path)


@pytest.fixture
def settings(tmp_path):
  pics = tmp_path / "pics"
  pics.mkdir()
  return config.Settings(
      pictures_dir=str(pics), thumbs_dir=str(tmp_path / "thumbs"),
      state_dir=str(tmp_path / "state"))


@pytest.fixture
def conn(settings):
  c = db.open_state(settings.state_dir)
  yield c
  c.close()
