FROM python:3.10-slim

# Install tools + libatomic1 (Node.js साठी)
RUN apt-get update && apt-get install -y curl git sudo bash unzip libatomic1 ca-certificates && \
    curl https://rclone.org/install.sh | bash && \
    rm -rf /var/lib/apt/lists/*

# PicoClaw (single static Go binary, very low RAM) - pick the asset for this CPU
# straight from the latest GitHub release, so no asset name is hardcoded.
ARG PICOCLAW_VERSION=latest
RUN set -e; \
    case "$(uname -m)" in \
      x86_64)  ARCH_RE='(x86_64|amd64)' ;; \
      aarch64) ARCH_RE='(arm64|aarch64)' ;; \
      *) echo "unsupported arch $(uname -m)"; exit 1 ;; \
    esac; \
    if [ "$PICOCLAW_VERSION" = "latest" ]; then API=https://api.github.com/repos/sipeed/picoclaw/releases/latest; \
    else API=https://api.github.com/repos/sipeed/picoclaw/releases/tags/$PICOCLAW_VERSION; fi; \
    URL=$(curl -fsSL "$API" | grep -o '"browser_download_url": *"[^"]*"' | cut -d'"' -f4 \
          | grep -i linux | grep -Ei "$ARCH_RE" | grep -E '\.tar\.gz$' | head -n1); \
    test -n "$URL"; echo "Installing PicoClaw from $URL"; \
    mkdir -p /tmp/pc && curl -fsSL "$URL" | tar -xz -C /tmp/pc; \
    install -m 0755 "$(find /tmp/pc -type f -name picoclaw | head -n1)" /usr/local/bin/picoclaw; \
    rm -rf /tmp/pc; picoclaw --help >/dev/null 2>&1 || true

WORKDIR /app

COPY keep_alive.py /app/keep_alive.py
COPY start.sh /app/start.sh
COPY setup_picoclaw.py /app/setup_picoclaw.py
RUN chmod +x /app/start.sh

COPY bin/ /app/bin/
RUN chmod +x /app/bin/*
ENV PATH="/app/bin:${PATH}"

# RAM optimization environment variables
ENV PYTHONOPTIMIZE=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV NODE_OPTIONS="--max-old-space-size=128"
ENV MALLOC_ARENA_MAX=2

EXPOSE 10000

CMD ["/app/start.sh"]
