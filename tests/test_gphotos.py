import pytest

from photoapp import gphotos
from tests.fake_gphotos import FakeGPhotos, TOKEN


@pytest.fixture
def fake():
  f = FakeGPhotos().start()
  yield f
  f.stop()


@pytest.fixture
def sleeps():
  return []


@pytest.fixture
def client(fake, sleeps):
  return gphotos.Client(fake.url, lambda: TOKEN, sleep=sleeps.append)


def write(tmp_path, name, data):
  p = tmp_path / name
  p.write_bytes(data)
  return str(p)


def test_upload_then_create_in_a_new_album(fake, client, tmp_path):
  tok = client.upload_bytes(write(tmp_path, "a b.jpg", b"AAA"), "image/jpeg")
  album = client.create_album("Trip 2024")
  res = client.batch_create([(tok, "caption")], album["id"])
  assert res[0]["ok"] and fake.items[res[0]["id"]]["filename"] == "a b.jpg"
  assert fake.items[res[0]["id"]]["description"] == "caption"
  assert fake.albums[album["id"]]["items"] == [res[0]["id"]]
  assert client.list_albums() == [{"id": album["id"], "title": "Trip 2024", "count": 1}]


def test_identical_bytes_are_one_media_item(fake, client, tmp_path):
  t1 = client.upload_bytes(write(tmp_path, "a.jpg", b"SAME"), "image/jpeg")
  t2 = client.upload_bytes(write(tmp_path, "b.jpg", b"SAME"), "image/jpeg")
  a = client.create_album("X")
  r1 = client.batch_create([(t1, None)], a["id"])
  r2 = client.batch_create([(t2, None)], a["id"])
  assert r1[0]["id"] == r2[0]["id"] and len(fake.items) == 1
  assert len(fake.albums[a["id"]]["items"]) == 1


def test_a_rejected_item_is_reported_not_raised(fake, client, tmp_path):
  fake.reject_filenames.add("bad.jpg")
  toks = [client.upload_bytes(write(tmp_path, n, n.encode()), "image/jpeg")
          for n in ("ok.jpg", "bad.jpg")]
  res = client.batch_create([(t, None) for t in toks])
  assert [r["ok"] for r in res] == [True, False] and res[1]["message"] == "rejected"


def test_batches_are_capped_at_fifty(client):
  with pytest.raises(ValueError):
    client.batch_create([("t", None)] * 51)


def test_429_waits_at_least_thirty_seconds_then_retries(fake, client, sleeps, tmp_path):
  fake.inject("/v1/uploads", [429, 429])
  tok = client.upload_bytes(write(tmp_path, "a.jpg", b"A"), "image/jpeg")
  assert tok and len(sleeps) == 2 and min(sleeps) >= 30 and sleeps[1] >= sleeps[0]


def test_500_is_retried_a_few_times_then_raised(fake, client, sleeps, tmp_path):
  fake.inject("/v1/albums", [500])
  assert client.create_album("ok")["title"] == "ok" and len(sleeps) == 1
  fake.inject("/v1/albums", [500] * 10)
  with pytest.raises(gphotos.GPhotosError) as e:
    client.create_album("never")
  assert e.value.status == 500 and len(sleeps) == 1 + gphotos.RETRY_5XX


def test_a_bad_token_raises_with_the_status(fake, sleeps):
  c = gphotos.Client(fake.url, lambda: "wrong", sleep=sleeps.append)
  with pytest.raises(gphotos.GPhotosError) as e:
    c.create_album("x")
  assert e.value.status == 401 and not sleeps


def test_albums_are_listed_across_pages_and_added_to_in_batches(fake, client):
  fake.page_size = 2
  ids = [client.create_album(f"A{i}")["id"] for i in range(5)]
  assert [a["title"] for a in client.list_albums()] == [f"A{i}" for i in range(5)]
  fake.items.update({f"m{i}": {"filename": "f", "sha": str(i), "mime": ""} for i in range(120)})
  client.add_to_album(ids[0], [f"m{i}" for i in range(120)])
  assert len(fake.albums[ids[0]]["items"]) == 120
