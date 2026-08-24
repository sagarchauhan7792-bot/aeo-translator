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

# --host 0.0.0.0 because Render's router needs the app listening on all
# interfaces, not just localhost, to forward traffic in. --no-browser because
# there is no local desktop here to open one on.
#
# --no-auth carries forward the choice already made for the local deployment
# (explicitly, twice) -- but a Render URL is PERMANENT and publicly indexable
# (certificate-transparency logs, search engines), unlike the local machine's
# ephemeral Cloudflare tunnel that changed on every restart. That is a real
# jump in exposure and worth reconsidering before this first deploy, not
# assumed to carry over silently. To add a password instead: upload an
# auth.json Secret File (run `python -m studio --set-password` locally first
# to generate one at aeo-translator/auth.json, then upload that file's
# contents as the Secret File named auth.json), and delete --no-auth below.
exec python -m studio --host 0.0.0.0 --port "${PORT:-8765}" --no-browser --no-auth
