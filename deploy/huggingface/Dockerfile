# Blog Studio as a Hugging Face Space (Docker SDK).
#
# This file, plus the README.md beside it, is the ENTIRE Space. It clones the
# app from GitHub at build time rather than the Space holding a copy of the
# code, which keeps one source of truth: push to GitHub, hit "Factory rebuild"
# here, and the Space is current. It also avoids force-pushing this repo over
# the Space's README, which is where a Space keeps its own configuration.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

# Spaces run the container as uid 1000, and nothing outside its home is
# writable. The app writes caches, drafts and reports next to its own code, so
# the checkout has to belong to that user.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH

WORKDIR /home/user/app
RUN git clone --depth 1 \
    https://github.com/sagarchauhan7792-bot/aeo-translator.git .
RUN pip install --no-cache-dir --user -r requirements.txt

# No browser on this host, so the Google OAuth consent flow must not be
# attempted -- gauth.py reads this and says what is missing instead.
ENV AEO_HEADLESS=1

# 7860 is the port a Space's router expects; it must match app_port in README.md.
EXPOSE 7860

# --no-auth because a Space is public by definition: there is no sign-in to hand
# out, and the whole point is that the URL just works. Nothing sensitive is
# mounted here -- no credentials are baked into this image and no Space secrets
# are read -- so what an open instance can spend is the free-tier quota of
# whatever keys are absent, which is nothing. Add keys as Space secrets only if
# you accept strangers spending them.
CMD ["python", "-m", "studio", "--host", "0.0.0.0", "--port", "7860", \
     "--no-browser", "--behind-proxy", "--no-auth", \
     "--allow-origin", "https://sagarchauhan7792-bot.github.io"]
