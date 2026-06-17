# Pocket-TTS Wyoming Protocol Server

Wyoming protocol server for [Pocket-TTS](https://github.com/kyutai-labs/pocket-tts), enabling Home Assistant integration with voice selection support.

## Quick Start with Docker Compose

Use the included `docker-compose.yml` file:

```yaml
services:
  pocket-tts-wyoming:
    image: ghcr.io/ikidd/pocket-tts-wyoming:latest
    container_name: pocket-tts-wyoming
    network_mode: host
    environment:
      - WYOMING_PORT=10201
      - WYOMING_HOST=0.0.0.0
      - DEFAULT_LANGUAGE=english
      - DEFAULT_VOICE=alba
      - VOICES_DIR=/voices
      - ZEROCONF=pocket-tts
    restart: unless-stopped
    volumes:
      - pocket-tts-hf-cache:/root/.cache/huggingface
      - pocket-tts-cache:/root/.cache/pocket_tts
      - ./voices:/voices

volumes:
  pocket-tts-hf-cache:
    driver: local
  pocket-tts-cache:
    driver: local
```

### Configuration

You can customize the following environment variables in the compose file before starting:

| Variable | Default | Description |
|----------|---------|-------------|
| `WYOMING_PORT` | `10201` | The port the Wyoming protocol server listens on. Change if you have a conflict with another service. |
| `WYOMING_HOST` | `0.0.0.0` | The network interface to bind to. `0.0.0.0` accepts connections from any interface. |
| `DEFAULT_LANGUAGE` | `english` | The default Pocket-TTS language/model to use when no voice language is specified. Use `french_24l` for French. |
| `DEFAULT_VOICE` | `alba` | The default voice used when none is specified. See [Available Voices](#available-voices) for options. |
| `MODEL_CONFIG` | unset | Optional local Pocket-TTS YAML config. Most users should leave this unset and use `DEFAULT_LANGUAGE`. |
| `VOICES_DIR` | `/voices` | Directory scanned for custom `.safetensors` voice states. |
| `ZEROCONF` | `pocket-tts` | Service name for mDNS/Zeroconf discovery. Home Assistant uses this to auto-discover the TTS server. Set to empty string to disable. |

Pull and start:

```bash
docker compose pull
docker compose up -d
```

The pre-built image is automatically updated via GitHub Actions when changes are pushed to the repository or when the upstream [Pocket-TTS](https://github.com/kyutai-labs/pocket-tts) repository is updated.

## Using the Pre-built Image

The image is available on GitHub Container Registry:

```bash
docker pull ghcr.io/ikidd/pocket-tts-wyoming:latest
```

## Running with Docker

```bash
docker run -d \
  --name pocket-tts-wyoming \
  --network host \
  -e DEFAULT_LANGUAGE=english \
  -e DEFAULT_VOICE=alba \
  -e VOICES_DIR=/voices \
  -e ZEROCONF=pocket-tts \
  -v pocket-tts-hf-cache:/root/.cache/huggingface \
  -v pocket-tts-cache:/root/.cache/pocket_tts \
  -v ./voices:/voices \
  ghcr.io/ikidd/pocket-tts-wyoming:latest
```

The volume mounts are recommended to cache model files and avoid re-downloads on restart.

## Building the Docker Image Manually

If you prefer to build the image locally instead of using the pre-built image:

```bash
docker build -t pocket-tts-wyoming .
```

Then update `docker-compose.yml` to use `build: .` instead of `image: ghcr.io/ikidd/pocket-tts-wyoming:latest`.


## Available Voices

English: alba, anna, azelma, bill_boerst, caro_davy, charles, cosette, eponine, eve, fantine, george, jane, javert, jean, marius, mary, michael, paul, peter_yearsley, stuart_bell, vera

French: estelle

German: juergen

Italian: giovanni

Portuguese: rafael

Spanish: lola

Pocket-TTS v2 loads one language model at a time. The server loads language models lazily based on the requested voice, so the first synthesis in a non-default language can take longer while the model is downloaded and initialized.

## Custom Voices

Custom Pocket-TTS voice states can be added by placing `.safetensors` files in
the `voices` directory mounted as `/voices` in the container. A file named
`voices/my_voice.safetensors` appears in Home Assistant as `my_voice`.

Custom voices use `DEFAULT_LANGUAGE` by default. To assign a custom voice to a
specific Pocket-TTS language/model, place it in a language subdirectory, for
example `voices/french_24l/ma_voix.safetensors`.

The server scans the voices directory when Home Assistant asks for Wyoming info
and before each synthesis request. If Home Assistant has already cached the
voice list, reload the Wyoming integration after adding a new file.

## Home Assistant Integration

The server supports Zeroconf/mDNS for automatic discovery.
Audio is streamed over the Wyoming protocol as Pocket-TTS produces chunks. When
clients stream text with `synthesize-start`/`synthesize-chunk`, complete
sentences are synthesized as soon as they are detected instead of waiting for
`synthesize-stop`. A small startup buffer is kept only long enough to detect and
trim the sacrificial prefix.

1. Start the Docker container
2. Go to Settings -> Devices & Services -> Add Integration
3. Search for "Wyoming Protocol"
4. The server should appear in the "Discovered" section, or enter `tcp://<server-ip>:10201` manually
5. Configure a Voice Assistant pipeline to use the TTS service and select a voice

## Debug Mode

Debug mode writes WAV files for each synthesis request and exposes timing tunables for diagnosing audio issues (such as the first word being cut off).

To run in debug mode, include the debug overlay file:

```bash
docker compose -f docker-compose.yml -f docker-compose.debug.yml up -d --build
```

This enables:
- **WAV file output**: Each synthesis writes a debug WAV file to the project directory
- **Timing tunables**: Environment variables to adjust the sacrificial prefix trimming

### Background

Audio-prompt based TTS models like Pocket-TTS can "swallow" the first word into a blend region when transitioning from the voice prompt. To prevent this, a sacrificial prefix (`"..."`) is prepended to all text and then trimmed from the resulting audio. Debug mode lets you tune this trimming.

Prefix trimming is streaming-aware: the server buffers only the beginning of the response until it finds the silence gap after the prefix, then immediately emits subsequent audio chunks. `PREFIX_MAX_DURATION` is therefore also the maximum amount of audio buffered before the server falls back to streaming without prefix trimming.

### Timing Tunables

| Variable | Default | Description |
|----------|---------|-------------|
| `PREFIX_MIN_DURATION` | `0.15` | Minimum seconds before looking for the pause after the prefix |
| `PREFIX_MAX_DURATION` | `1.0` | Maximum seconds to search for the prefix end |
| `PREFIX_SILENCE_GAP` | `0.08` | Minimum silence duration (seconds) to identify the gap after the prefix |
| `PREFIX_KEEP_BEFORE` | `0.08` | Seconds of audio kept before the detected prefix end to avoid cutting the first syllable |
| `END_PADDING` | `0.12` | Seconds of silence appended before `AudioStop` to avoid client-side clipping |

**Tuning tips:**
- If you hear part of the "..." prefix, decrease `PREFIX_SILENCE_GAP` to catch shorter pauses
- If the first syllable is still being cut, increase `PREFIX_KEEP_BEFORE` first, then `PREFIX_MIN_DURATION`
- If the end of the sentence is cut, increase `END_PADDING`
- Different voices speak at different speeds, so optimal values may vary

## Troubleshooting

- **Slow startup**: First run downloads ~500MB of model weights. Use volume mounts to persist the cache.
- **Connection issues**: Verify port 10201 is open and check logs with `docker compose logs pocket-tts-wyoming` or `docker logs pocket-tts-wyoming`
- **Voice not found**: Ensure the voice name matches one of the predefined voices listed above.
- **Image pull issues**: If you encounter authentication issues pulling from GHCR, ensure you're logged in: `docker login ghcr.io`
- **Outdated image**: Pull the latest image with `docker compose pull` or `docker pull ghcr.io/ikidd/pocket-tts-wyoming:latest`
- **First word cut off**: Run in debug mode and check the WAV files. Adjust the timing tunables as needed.

- **⏳ Last Build On**: Never
- **🔄 Last Run**: 2026-01-20 00:30:58 UTC
- **Last Upstream SHA**: 6f9dd250c24ee85cecc5587902a684f0d82b2a0d 
## 📅 Release Status
- **⏳ Last Build On**: 2026-06-17 00:52:28 UTC
- **🔄 Last Run**: 2026-06-17 00:52:28 UTC
- **Last Upstream SHA**: 15a6c1817b360f9b37691aef9734435a85610c68
