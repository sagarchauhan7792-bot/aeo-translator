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

if [ ! -e auth.json ]; then
  echo "render_start: no auth.json Secret File -- the app will refuse to start."
  echo "render_start: run \`python -m studio --set-password\` locally, then upload"
  echo "render_start: the auth.json it writes as a Secret File named auth.json."
fi

# --host 0.0.0.0 because Render's router needs the app listening on all
# interfaces, not just localhost, to forward traffic in. --no-browser because
# there is no local desktop here to open one on. --behind-proxy because Render
# terminates TLS in front of us: without it the session cookie is issued without
# the Secure flag, and every sign-in attempt looks like it came from the proxy,
# so the throttle counts all visitors as one.
#
# There is deliberately no --no-auth here. A Render URL is PERMANENT and
# publicly indexable (certificate-transparency logs, search engines), unlike the
# ephemeral Cloudflare tunnel that changed on every restart -- and this app holds
# live API keys, spends Gemini quota, crawls whatever it is pointed at and can
# write to Drive. Upload auth.json and it asks for the password you already set.
# If you truly want it open to anyone who finds the URL, add --no-auth below
# yourself, knowing that is what it does.
exec python -m studio --host 0.0.0.0 --port "${PORT:-8765}" \
     --no-browser --behind-proxy
