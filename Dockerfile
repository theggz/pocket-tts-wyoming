FROM ghcr.io/astral-sh/uv:debian

WORKDIR /app

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/kyutai-labs/pocket-tts.git .

COPY wyoming_tts_server.py .

RUN uv add "wyoming>=1.8,<2" zeroconf "sentence-stream>=1.2.0,<2"
RUN mkdir -p /voices

ENV WYOMING_PORT=10201
ENV WYOMING_HOST=0.0.0.0
ENV DEFAULT_LANGUAGE=english
ENV DEFAULT_VOICE=alba
ENV VOICES_DIR=/voices
ENV PYTHONUNBUFFERED=1

EXPOSE 10201

CMD ["uv", "run", "python", "wyoming_tts_server.py"]
