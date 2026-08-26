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

for f in credentials.json token.json gemini_api_key.txt openai_api_key.txt \
         openai_base_url.txt openai_model.txt pagespeed_api_key.txt \
         bhashini_user_id.txt bhashini_api_key.txt google-ads.yaml \
         google_ads_customer_id.txt auth.json; do
  if [ -f "/etc/secrets/$f" ] && [ ! -e "$f" ]; then
    ln -s "/etc/secrets/$f" "$f"
    echo "render_start: linked $f from Secret Files"
  fi
done

# The instance is open: anyone with the URL gets the tool, which is the point of
# publishing it. OPEN=0 puts a sign-in in front of the whole app instead.
OPEN="${OPEN:-1}"
AUTH_FLAG="--no-auth"
if [ "$OPEN" = "0" ]; then
  AUTH_FLAG=""
  if [ ! -e auth.json ]; then
    echo "render_start: OPEN=0 but no auth.json Secret File -- the app will refuse"
    echo "render_start: to start. Run 'python -m studio --set-password' locally and"
    echo "render_start: upload the auth.json it writes as a Secret File."
  fi
fi

# Open does not have to mean open to everything. These actions spend the
# owner's quota or write to the owner's Drive, so they sit behind the password
# while the audit, ideas, crawl, GEO, compare and report tabs stay public. That
# is what lets this instance carry the same credentials as the laptop without
# handing them to whoever finds the URL. PROTECT= (empty) opens everything.
PROTECT="${PROTECT:-translate,draft,fix}"
if [ -n "$PROTECT" ]; then
  if [ -e auth.json ]; then
    echo "render_start: sign-in required for: $PROTECT"
  else
    echo "render_start: WARNING -- PROTECT names $PROTECT but no auth.json is"
    echo "render_start: mounted, so nobody can sign in and those actions are"
    echo "render_start: unusable. Upload auth.json, or set PROTECT= to open them."
  fi
  PROTECT_FLAG="--protect $PROTECT"
else
  PROTECT_FLAG=""
  echo "render_start: PROTECT is empty -- every action is open to everyone."
fi

# Origin of the static copy of index.html on GitHub Pages, so that page can
# drive this backend. Visitors using this service's own URL are same-origin and
# never need it.
ALLOW_ORIGIN="${ALLOW_ORIGIN:-https://sagarchauhan7792-bot.github.io}"

# --host 0.0.0.0 because Render's router needs the app listening on all
# interfaces. --no-browser because there is no desktop here. --behind-proxy
# because Render terminates TLS in front of us: without it the session cookie
# goes out missing its Secure flag and every sign-in looks like it came from
# the proxy, collapsing the per-IP throttle into one shared counter.
exec python -m studio --host 0.0.0.0 --port "${PORT:-8765}" \
     --no-browser --behind-proxy $AUTH_FLAG $PROTECT_FLAG \
     --allow-origin "$ALLOW_ORIGIN"
