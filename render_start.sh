#!/bin/bash
# Render-specific startup shim -- deliberately kept OUT of the app itself, so
# nothing in common.py/server.py needs to know it might be running on Render.
#
# Render's "Secret Files" feature mounts uploaded files at /etc/secrets/<name>,
# not in the app's working directory. This links each expected credential file
# into place if you've added it as a Secret File in the Render dashboard, using
# exactly the plain filename the app already looks for -- so nothing in the
# Python code needs to change to find them.
set -e

for f in credentials.json token.json gemini_api_key.txt pagespeed_api_key.txt \
         bhashini_user_id.txt bhashini_api_key.txt google-ads.yaml \
         google_ads_customer_id.txt auth.json; do
  if [ -f "/etc/secrets/$f" ] && [ ! -e "$f" ]; then
    ln -s "/etc/secrets/$f" "$f"
    echo "render_start: linked $f from Secret Files"
  fi
done

# The public instance is deliberately open: the point of it is that anyone can
# open the URL and use the tool without installing anything or being let in.
# Set OPEN=0 in the Render dashboard to put the sign-in back (upload an
# auth.json Secret File first, from `python -m studio --set-password` locally).
OPEN="${OPEN:-1}"
AUTH_FLAG="--no-auth"
if [ "$OPEN" = "0" ]; then
  AUTH_FLAG=""
  if [ ! -e auth.json ]; then
    echo "render_start: OPEN=0 but no auth.json Secret File -- the app will refuse"
    echo "render_start: to start. Run 'python -m studio --set-password' locally and"
    echo "render_start: upload the auth.json it writes as a Secret File."
  fi
else
  echo "render_start: OPEN -- no sign-in. Anyone with the URL gets the crawler,"
  echo "render_start: the writer quota, and whatever Secret Files are mounted."
fi

# Origin of the static copy of index.html on GitHub Pages, so that page can
# drive this backend. Visitors using this service's own URL are same-origin and
# never need it.
ALLOW_ORIGIN="${ALLOW_ORIGIN:-https://sagarchauhan7792-bot.github.io}"

# --host 0.0.0.0 because Render's router needs the app listening on all
# interfaces, not just localhost, to forward traffic in. --no-browser because
# there is no local desktop here to open one on. --behind-proxy because Render
# terminates TLS in front of us: without it the session cookie is issued without
# the Secure flag, and every sign-in attempt looks like it came from the proxy,
# so the throttle counts all visitors as one.
#
# A Render URL is PERMANENT and publicly indexable (certificate-transparency
# logs, search engines), so running open is a decision rather than a default
# carried over from the local setup: whoever finds it gets the crawler, the
# writer quota, and Drive write access if credentials.json and token.json are
# mounted. The safe shape of an open instance is to mount NO Google credentials
# -- the audit, ideas, site crawl and compare tabs all run without any -- and to
# add only the keys whose public use is acceptable.
exec python -m studio --host 0.0.0.0 --port "${PORT:-8765}" \
     --no-browser --behind-proxy $AUTH_FLAG --allow-origin "$ALLOW_ORIGIN"
