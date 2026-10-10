"""One-time Google Photos authorization (ticket 210).

  python -m photoapp.google_auth

Prints a URL (and tries to open it); approve in the browser, which redirects back to a short-lived
server on 127.0.0.1 of THIS machine, so run it where you can open a browser (or forward the printed
port). Stores a refresh token in --gphotos_token_file. See docs/google-photos.md.
"""

import sys
import webbrowser

from absl import app

from photoapp import config
from photoapp import gphotos_auth


def main(argv):
  settings = config.Settings.load(check_pictures_dir=False)

  def show(url):
    print("Open this URL in a browser and approve access:\n\n  " + url + "\n", flush=True)
    try:
      webbrowser.open(url)
    except Exception:
      pass

  try:
    gphotos_auth.authorize(settings.gphotos_client_path, settings.gphotos_token_path, show)
  except gphotos_auth.AuthError as e:
    sys.exit(f"google_auth: {e}")
  print(f"Authorized. Token stored in {settings.gphotos_token_path}")


if __name__ == "__main__":
  app.run(main)
