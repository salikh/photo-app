"""One-time Google Photos authorization (ticket 210).

  python -m photoapp.google_auth

Prints a URL; open it in any browser and approve. The browser then tries to load
http://127.0.0.1:1/?code=... and shows a connection error: copy the code (or the whole address) from
the address bar and paste it at the prompt. Works over ssh. Stores a refresh token in
--gphotos_token_file. See docs/google-photos.md.
"""

import sys

from absl import app

from photoapp import config
from photoapp import gphotos_auth


def main(argv):
  settings = config.Settings.load(check_pictures_dir=False)

  def show(url):
    print("Open this URL in a browser and approve access:\n\n  " + url + "\n\n"
          "The browser will then fail to load a http://127.0.0.1:1/?code=... page; that is expected.",
          flush=True)

  def read_code():
    return input("Paste the code (or the full address from the address bar): ")

  try:
    gphotos_auth.authorize(settings.gphotos_client_path, settings.gphotos_token_path, show,
                           read_code)
  except gphotos_auth.AuthError as e:
    sys.exit(f"google_auth: {e}")
  print(f"Authorized. Token stored in {settings.gphotos_token_path}")


if __name__ == "__main__":
  app.run(main)
