FROM docker.io/tailscale/tailscale:stable AS tailscale

# PicoClaw built from source WITH our summarisation fix (patches/picoclaw-summary-fix.patch).
# Best effort: if cloning / patching / building fails (network, low build RAM) /out stays empty and the final image
# simply keeps the unpatched release binary installed below.
FROM docker.io/library/golang:1.25-bookworm AS picobuild
ARG PICOCLAW_VERSION=v0.3.1
ARG PICOCLAW_PATCH=on
COPY patches/ /patches/
ENV CGO_ENABLED=0 GOMEMLIMIT=1200MiB
RUN mkdir -p /out; \
    if [ "$PICOCLAW_PATCH" = "on" ]; then \
      ( set -e; \
        git clone --depth 1 --branch "$PICOCLAW_VERSION" https://github.com/sipeed/picoclaw.git /src; \
        cd /src; \
        git apply --check /patches/picoclaw-summary-fix.patch; \
        git apply /patches/picoclaw-summary-fix.patch; \
        go build -p 1 -tags goolm,stdjson -ldflags "-s -w" -o /out/picoclaw ./cmd/picoclaw; \
        /out/picoclaw --help >/dev/null 2>&1 || true \
      ) || { echo "### PATCHED PICOCLAW BUILD FAILED - using the unpatched release binary"; rm -f /out/picoclaw; }; \
    else echo "PICOCLAW_PATCH=$PICOCLAW_PATCH - skipping patched build"; fi

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

# Swap in the patched binary when the picobuild stage produced one.
COPY --from=picobuild /out/ /tmp/picoclaw-patched/
RUN if [ -x /tmp/picoclaw-patched/picoclaw ]; then \
      install -m 0755 /tmp/picoclaw-patched/picoclaw /usr/local/bin/picoclaw; \
      echo "${PICOCLAW_VERSION}+summaryfix" > /etc/picoclaw-version; \
      echo "Using PATCHED PicoClaw ${PICOCLAW_VERSION}+summaryfix"; \
    else echo "Using unpatched PicoClaw ${PICOCLAW_VERSION}"; fi; \
    rm -rf /tmp/picoclaw-patched

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
COPY fsd.py /app/fsd.py
COPY sysstats.py /app/sysstats.py
COPY restore_progress.py /app/restore_progress.py
COPY backup_status.py /app/backup_status.py
COPY ram_guard.py /app/ram_guard.py
COPY start.sh /app/start.sh
COPY setup_picoclaw.py /app/setup_picoclaw.py
COPY llm_proxy.py /app/llm_proxy.py
COPY notify.py /app/notify.py
COPY log_command.py /app/log_command.py
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
