FROM python:3.10-slim

# Install necessary tools & rclone
RUN apt-get update && apt-get install -y curl git sudo bash unzip && \
    curl https://rclone.org/install.sh | bash && \
    rm -rf /var/lib/apt/lists/*

# Install Hermes Agent
RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

# ── Build time cleanup — disk वाचवण्यासाठी (Koyeb 2GB limit)
# हे files runtime ला लागत नाहीत, पण install script सोबत येतात
RUN set -e && \
    # Cache folders — temporary, runtime ला regenerate होतात
    rm -rf /root/.hermes/cache \
           /root/.hermes/audio_cache \
           /root/.hermes/image_cache \
           /root/.hermes/tmp && \
    # Python bytecode — Python आपोआप परत बनवतो
    find /root/.hermes -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true && \
    find /root/.hermes -name "*.pyc" -delete 2>/dev/null || true && \
    # pip cache
    rm -rf /root/.cache/pip && \
    # apt cache (already cleaned above but just in case)
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

WORKDIR /app

# Copy our custom scripts
COPY keep_alive.py /app/keep_alive.py
COPY start.sh /app/start.sh
COPY watchdog.sh /app/watchdog.sh
RUN chmod +x /app/start.sh /app/watchdog.sh

# Wrapper scripts that shadow apt-get/apt/pip/pip3/npm: they run the real
# tool as usual, then log any successful `install` into
# ~/.hermes/installed/*.list so start.sh can reinstall the same
# packages/tools automatically after a restart (that list rides along with
# the normal Google Drive backup of ~/.hermes).
COPY bin/ /app/bin/
RUN chmod +x /app/bin/*
ENV PATH="/app/bin:${PATH}"

EXPOSE 10000

CMD ["/app/start.sh"]
