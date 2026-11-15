FROM docker.io/tailscale/tailscale:stable AS tailscale

FROM python:3.10-slim

# Install tools + libatomic1 (Node.js साठी)
RUN apt-get update && apt-get install -y curl git sudo bash unzip libatomic1 ca-certificates && \
    curl https://rclone.org/install.sh | bash && \
    rm -rf /var/lib/apt/lists/*

# PicoClaw (single static Go binary, very low RAM) - PINNED version, so every rebuild/restart/redeploy gets
# exactly the same binary. Downloaded straight from the release URL: no GitHub API call (no rate limit, no JSON parsing).
# To move to another version change ONLY this line (or --build-arg PICOCLAW_VERSION=vX.Y.Z), after reading the release notes.
ARG PICOCLAW_VERSION=v0.3.1
RUN set -e; \
    case "$(uname -m)" in \
      x86_64)  ASSET=picoclaw_Linux_x86_64.tar.gz ;; \
      aarch64) ASSET=picoclaw_Linux_arm64.tar.gz ;; \
      *) echo "unsupported arch $(uname -m)"; exit 1 ;; \
    esac; \
    case "$PICOCLAW_VERSION" in latest|"") echo "PICOCLAW_VERSION must be a fixed tag like v0.3.1, not 'latest'"; exit 1 ;; esac; \
    URL="https://github.com/sipeed/picoclaw/releases/download/${PICOCLAW_VERSION}/${ASSET}"; \
    echo "Installing PicoClaw ${PICOCLAW_VERSION} from $URL"; \
    mkdir -p /tmp/pc && curl -fsSL --retry 5 --retry-delay 3 "$URL" | tar -xz -C /tmp/pc; \
    install -m 0755 "$(find /tmp/pc -type f -name picoclaw | head -n1)" /usr/local/bin/picoclaw; \
    rm -rf /tmp/pc; echo "$PICOCLAW_VERSION" > /etc/picoclaw-version; \
    picoclaw --help >/dev/null 2>&1 || true

# Playwright + Chromium, baked into the image (so it survives restarts).
# --with-deps pulls the system libs Chromium needs.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw
RUN pip install --no-cache-dir requests beautifulsoup4 playwright && \
    playwright install --with-deps chromium && \
    rm -rf /var/lib/apt/lists/* /root/.cache/pip

# Tailscale (official static binaries, run in userspace mode - no TUN device needed)
COPY --from=tailscale /usr/local/bin/tailscaled /usr/local/bin/tailscaled
COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscale

WORKDIR /app

COPY keep_alive.py /app/keep_alive.py
COPY sysstats.py /app/sysstats.py
COPY start.sh /app/start.sh
COPY setup_picoclaw.py /app/setup_picoclaw.py
COPY llm_proxy.py /app/llm_proxy.py
COPY notify.py /app/notify.py
RUN chmod +x /app/start.sh

COPY bin/ /app/bin/
RUN chmod +x /app/bin/*
ENV PATH="/app/bin:${PATH}"

# RAM optimization environment variables
ENV PYTHONOPTIMIZE=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV NODE_OPTIONS="--max-old-space-size=256"
ENV MALLOC_ARENA_MAX=2

EXPOSE 10000

CMD ["/app/start.sh"]
