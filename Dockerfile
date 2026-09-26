FROM python:3.10-slim

# Install tools + libatomic1 (Node.js साठी)
RUN apt-get update && apt-get install -y curl git sudo bash unzip libatomic1 && \
    curl https://rclone.org/install.sh | bash && \
    rm -rf /var/lib/apt/lists/*

# Install Hermes Agent
RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

# ── FIX: Hermes च्या bundled python मध्ये PyYAML install कर ──
# start.sh मधला config.yaml patch script याच python ने चालतो. yaml module
# नसेल तर तो नेहमी crude regex fallback वर जातो, जो nested corruption
# (उदा. "model: auto" खाली orphaned "provider:" key) पूर्ण साफ करत नाही —
# त्यामुळेच "expected <block end>, but found '<block mapping start>'" error
# आणि Telegram adapter creation fail होत राहतं. यामुळे real yaml parse+patch
# path चालेल.
RUN HERMES_PY=$(ls /root/.hermes/tools/python-*/bin/python3 2>/dev/null | sort -V | tail -1) && \
    if [ -n "$HERMES_PY" ]; then \
        "$HERMES_PY" -m pip install --no-cache-dir pyyaml; \
    else \
        echo "WARNING: Hermes bundled python not found, pyyaml not installed"; \
    fi

# Build time cleanup — disk वाचवण्यासाठी (Koyeb 2GB limit)
RUN set -e && \
    rm -rf /root/.hermes/cache \
           /root/.hermes/audio_cache \
           /root/.hermes/image_cache \
           /root/.hermes/tmp && \
    find /root/.hermes -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true && \
    find /root/.hermes -name "*.pyc" -delete 2>/dev/null || true && \
    rm -rf /root/.cache/pip && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

WORKDIR /app

COPY keep_alive.py /app/keep_alive.py
COPY start.sh /app/start.sh
COPY watchdog.sh /app/watchdog.sh
RUN chmod +x /app/start.sh /app/watchdog.sh

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
