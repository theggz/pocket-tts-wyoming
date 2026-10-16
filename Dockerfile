FROM ghcr.io/astral-sh/uv:debian
WORKDIR /app
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch v3.3.0 https://github.com/kyutai-labs/pocket-tts.git . && rm -rf .git
COPY wyoming_tts_server.py .

# UV_TORCH_BACKEND has no effect on `uv add`/`uv lock`/`uv sync`/`uv run` —
# it only applies to the `uv pip` / `uv tool` interfaces. For project
# workflows, the CPU index has to be pinned in pyproject.toml instead.
RUN printf '\n[[tool.uv.index]]\nname = "pytorch-cpu"\nurl = "https://download.pytorch.org/whl/cpu"\nexplicit = true\n\n[tool.uv.sources]\ntorch = { index = "pytorch-cpu" }\n' >> pyproject.toml

RUN rm -f uv.lock
RUN uv add "wyoming>=1.8,<2" zeroconf "sentence-stream>=1.2.0,<2"
RUN uv lock
RUN uv sync --frozen
RUN mkdir -p /voices

ENV WYOMING_PORT=10201
ENV WYOMING_HOST=0.0.0.0
ENV DEFAULT_LANGUAGE=english
ENV DEFAULT_VOICE=alba
ENV VOICES_DIR=/voices
ENV QUANTIZE=false
ENV PYTHONUNBUFFERED=1

EXPOSE 10201

CMD ["uv", "run", "--no-sync", "python", "wyoming_tts_server.py"]