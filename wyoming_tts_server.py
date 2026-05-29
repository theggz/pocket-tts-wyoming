#!/usr/bin/env python3
"""
Wyoming Protocol TTS Server for Pocket-TTS

Implements Wyoming protocol TTS server that wraps pocket-tts,
exposing available voices to Home Assistant for selection.
"""

import argparse
import asyncio
import logging
import os
import time
import wave
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Optional

import numpy

from pocket_tts import TTSModel
try:
    from sentence_stream import SentenceBoundaryDetector
except ImportError:
    class SentenceBoundaryDetector:
        """Small fallback used when sentence-stream is not installed locally."""

        def __init__(self) -> None:
            self._buffer = ""

        def add_chunk(self, text: str):
            self._buffer += text
            sentences = []
            start = 0
            for index, char in enumerate(self._buffer):
                if char in ".!?":
                    sentence = self._buffer[start : index + 1].strip()
                    if sentence:
                        sentences.append(sentence)
                    start = index + 1

            self._buffer = self._buffer[start:]
            return sentences

        def finish(self) -> str:
            text = self._buffer.strip()
            self._buffer = ""
            return text

try:
    from pocket_tts.default_parameters import (
        DEFAULT_LANGUAGE as POCKET_TTS_DEFAULT_LANGUAGE,
        get_default_voice_for_language,
    )
    from pocket_tts.utils.utils import _ORIGINS_OF_PREDEFINED_VOICES
except ImportError:
    from pocket_tts.default_parameters import DEFAULT_VARIANT as POCKET_TTS_DEFAULT_LANGUAGE
    from pocket_tts.utils.utils import PREDEFINED_VOICES as _ORIGINS_OF_PREDEFINED_VOICES

    def get_default_voice_for_language(language: str | None) -> str:
        return "alba"
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncServer, AsyncTcpServer
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeStopped,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = int(os.environ.get("WYOMING_PORT", "10201"))
DEFAULT_LANGUAGE = os.environ.get(
    "DEFAULT_LANGUAGE",
    os.environ.get("MODEL_LANGUAGE", POCKET_TTS_DEFAULT_LANGUAGE),
)
DEFAULT_VOICE = os.environ.get(
    "DEFAULT_VOICE", get_default_voice_for_language(DEFAULT_LANGUAGE)
)
MODEL_CONFIG = os.environ.get("MODEL_CONFIG")
MODEL_VARIANT = os.environ.get("MODEL_VARIANT")
DEBUG_WAV = os.environ.get("DEBUG_WAV", "").lower() in ("true", "1", "yes")

LANGUAGE_ALIASES = {
    "en": "english",
    "en-us": "english",
    "en-gb": "english",
    "english": "english",
    "english_2026-01": "english_2026-01",
    "english_2026-04": "english_2026-04",
    "fr": "french_24l",
    "fr-fr": "french_24l",
    "french": "french_24l",
    "french_24l": "french_24l",
    "de": "german_24l",
    "de-de": "german_24l",
    "german": "german_24l",
    "german_24l": "german_24l",
    "pt": "portuguese",
    "pt-pt": "portuguese",
    "pt-br": "portuguese",
    "portuguese": "portuguese",
    "it": "italian",
    "it-it": "italian",
    "italian": "italian",
    "italian_24l": "italian_24l",
    "es": "spanish_24l",
    "es-es": "spanish_24l",
    "spanish": "spanish_24l",
    "spanish_24l": "spanish_24l",
}

LANGUAGE_CODES = {
    "english": "en",
    "english_2026-01": "en",
    "english_2026-04": "en",
    "french_24l": "fr",
    "german_24l": "de",
    "portuguese": "pt",
    "italian": "it",
    "italian_24l": "it",
    "spanish_24l": "es",
}

VOICE_LANGUAGES = {
    "giovanni": "italian",
    "lola": "spanish_24l",
    "juergen": "german_24l",
    "rafael": "portuguese",
    "estelle": "french_24l",
}

PREDEFINED_VOICES = dict(_ORIGINS_OF_PREDEFINED_VOICES)
for _voice_name in PREDEFINED_VOICES:
    VOICE_LANGUAGES.setdefault(_voice_name, "english")

# Prefix trimming tunables (in seconds)
# Minimum time before looking for the pause after the sacrificial prefix
PREFIX_MIN_DURATION = float(os.environ.get("PREFIX_MIN_DURATION", "0.15"))
# Maximum time to search for the prefix end
PREFIX_MAX_DURATION = float(os.environ.get("PREFIX_MAX_DURATION", "1.0"))
# Minimum silence duration to consider it the gap after the prefix
PREFIX_SILENCE_GAP = float(os.environ.get("PREFIX_SILENCE_GAP", "0.08"))

_VOICE_STATES: dict[str, dict] = {}
_MODELS: dict[str, TTSModel] = {}
_VOICE_LOCK = asyncio.Lock()


def normalize_language(language: str | None) -> str:
    """Normalize Home Assistant/Wyoming language values to Pocket-TTS model names."""
    if not language:
        return normalize_language(DEFAULT_LANGUAGE)

    normalized = language.lower().replace("_", "-")
    return LANGUAGE_ALIASES.get(normalized, LANGUAGE_ALIASES.get(language.lower(), language))


def is_language_name(value: str) -> bool:
    normalized = value.lower().replace("_", "-")
    return normalized in LANGUAGE_ALIASES or value.lower() in LANGUAGE_ALIASES


def get_voice_language(voice_name: str) -> str:
    return normalize_language(VOICE_LANGUAGES.get(voice_name, DEFAULT_LANGUAGE))


def default_voice_for_language(language: str | None) -> str:
    normalized_language = normalize_language(language)
    return get_default_voice_for_language(normalized_language.replace("_24l", ""))


def load_tts_model(language: str) -> TTSModel:
    config = MODEL_CONFIG
    if not config and MODEL_VARIANT and Path(MODEL_VARIANT).suffix in (".yaml", ".yml"):
        config = MODEL_VARIANT

    if config:
        return TTSModel.load_model(config=config)

    try:
        return TTSModel.load_model(language=language)
    except TypeError:
        return TTSModel.load_model(config=MODEL_VARIANT or language)


def find_prefix_end(
    audio: numpy.ndarray,
    sample_rate: int,
    threshold: float,
) -> int:
    """Find the silence gap after the sacrificial prefix."""
    min_prefix_samples = int(sample_rate * PREFIX_MIN_DURATION)
    max_prefix_samples = int(sample_rate * PREFIX_MAX_DURATION)
    min_silence_samples = int(sample_rate * PREFIX_SILENCE_GAP)

    if len(audio) <= min_prefix_samples:
        return 0

    search_end = min(len(audio), max_prefix_samples)
    is_silent = numpy.abs(audio[:search_end]) < threshold

    i = min_prefix_samples
    while i < search_end:
        if is_silent[i]:
            silence_start = i
            while i < search_end and is_silent[i]:
                i += 1

            if (i - silence_start) >= min_silence_samples:
                return i
        else:
            i += 1

    return 0


class PocketTTSEventHandler(AsyncEventHandler):
    """Event handler for Pocket-TTS Wyoming server."""

    def __init__(
        self,
        wyoming_info: Info,
        cli_args: argparse.Namespace,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.cli_args = cli_args
        self.wyoming_info_event = wyoming_info.event()
        self.is_streaming: Optional[bool] = None
        self.sbd = SentenceBoundaryDetector()
        self._synthesize: Optional[Synthesize] = None

    async def handle_event(self, event: Event) -> bool:
        """Handle incoming Wyoming protocol events."""
        if Describe.is_type(event.type):
            await self.write_event(self.wyoming_info_event)
            _LOGGER.debug("Sent info")
            return True

        try:
            if Synthesize.is_type(event.type):
                if self.is_streaming:
                    # Ignore since this is only sent for compatibility reasons.
                    # For streaming, we expect:
                    # [synthesize-start] -> [synthesize-chunk]+ -> [synthesize]? -> [synthesize-stop]
                    return True

                synthesize = Synthesize.from_event(event)
                self._synthesize = Synthesize(text="", voice=synthesize.voice)
                self.sbd = SentenceBoundaryDetector()
                start_sent = False

                for i, sentence in enumerate(self.sbd.add_chunk(synthesize.text)):
                    self._synthesize.text = sentence
                    await self._handle_synthesize(
                        self._synthesize, send_start=(i == 0), send_stop=False
                    )
                    start_sent = True

                self._synthesize.text = self.sbd.finish()
                if self._synthesize.text:
                    await self._handle_synthesize(
                        self._synthesize,
                        send_start=(not start_sent),
                        send_stop=True,
                    )
                else:
                    await self.write_event(AudioStop().event())

                return True

            if SynthesizeStart.is_type(event.type):
                stream_start = SynthesizeStart.from_event(event)
                self.is_streaming = True
                self.sbd = SentenceBoundaryDetector()
                self._synthesize = Synthesize(text="", voice=stream_start.voice)
                _LOGGER.debug("Text stream started: voice=%s", stream_start.voice)
                return True

            if SynthesizeChunk.is_type(event.type):
                assert self._synthesize is not None
                stream_chunk = SynthesizeChunk.from_event(event)
                _LOGGER.debug("Received stream chunk: %s", stream_chunk.text[:50])
                for sentence in self.sbd.add_chunk(stream_chunk.text):
                    _LOGGER.debug("Synthesizing stream sentence: %s", sentence)
                    self._synthesize.text = sentence
                    await self._handle_synthesize(self._synthesize)

                return True

            if SynthesizeStop.is_type(event.type):
                assert self._synthesize is not None
                self._synthesize.text = self.sbd.finish()
                if self._synthesize.text:
                    await self._handle_synthesize(self._synthesize)

                await self.write_event(SynthesizeStopped().event())
                self.is_streaming = False
                _LOGGER.debug("Text stream stopped")
                return True

            return True
        except Exception as err:
            await self.write_event(
                Error(text=str(err), code=err.__class__.__name__).event()
            )
            raise err

    async def _handle_synthesize(
        self, synthesize: Synthesize, send_start: bool = True, send_stop: bool = True
    ) -> bool:
        """Handle synthesis request."""
        _LOGGER.debug(synthesize)

        raw_text = synthesize.text
        text = " ".join(raw_text.strip().splitlines())

        if not text:
            _LOGGER.warning("Empty text received")
            if send_stop:
                await self.write_event(AudioStop().event())
            return True

        _LOGGER.debug("synthesize: raw_text=%s, text='%s'", raw_text, text)
        
        # Add a sacrificial prefix to prevent the first word from being swallowed
        # by the voice prompt "blend region". This prefix audio will be trimmed later.
        text = "... " + text
        
        voice_name: Optional[str] = None
        requested_language: Optional[str] = None

        if synthesize.voice is not None:
            voice_name = synthesize.voice.name
            requested_language = synthesize.voice.language

        if voice_name is None:
            voice_name = self.cli_args.voice

        # Extract voice name from model name if it's in format "pocket-tts-{voice}"
        if voice_name and voice_name.startswith("pocket-tts-"):
            voice_name = voice_name.replace("pocket-tts-", "", 1)

        if voice_name and is_language_name(voice_name):
            requested_language = voice_name
            voice_name = default_voice_for_language(requested_language)

        if voice_name not in PREDEFINED_VOICES:
            _LOGGER.warning(
                "Voice '%s' not found, using default '%s'", voice_name, self.cli_args.voice
            )
            voice_name = self.cli_args.voice

        assert voice_name is not None
        language = normalize_language(requested_language or get_voice_language(voice_name))

        async with _VOICE_LOCK:
            global _VOICE_STATES
            global _MODELS

            if language not in _MODELS:
                _LOGGER.info("Loading Pocket-TTS model (language: %s)...", language)
                _MODELS[language] = load_tts_model(language)
                _LOGGER.info("Model loaded successfully for language: %s", language)
                _LOGGER.info("Sample rate: %d Hz", _MODELS[language].sample_rate)

            tts_model = _MODELS[language]
            voice_state_key = f"{language}:{voice_name}"

            if voice_state_key not in _VOICE_STATES:
                _LOGGER.info("Loading voice state for: %s (%s)", voice_name, language)
                try:
                    _VOICE_STATES[voice_state_key] = tts_model.get_state_for_audio_prompt(
                        voice_name
                    )
                except Exception as e:
                    _LOGGER.error("Failed to load voice state for %s: %s", voice_name, e)
                    await self.write_event(
                        Error(
                            text=f"Failed to load voice: {voice_name}",
                            code="VoiceLoadError",
                        ).event()
                    )
                    return True

            voice_state = _VOICE_STATES[voice_state_key]

            try:
                _LOGGER.info(
                    "Synthesizing text (voice: %s, language: %s, length: %d chars)",
                    voice_name,
                    language,
                    len(text),
                )

                sample_rate = tts_model.sample_rate
                width = 2
                channels = 1
                bytes_per_sample = width * channels
                samples_per_chunk = 1024
                bytes_per_chunk = bytes_per_sample * samples_per_chunk

                if send_start:
                    await self.write_event(
                        AudioStart(
                            rate=sample_rate,
                            width=width,
                            channels=channels,
                        ).event(),
                    )

                audio_chunks = tts_model.generate_audio_stream(
                    model_state=voice_state, text_to_generate=text, copy_state=True
                )

                debug_audio_arrays = []
                pending_bytes = b""
                synthesis_started_at = time.monotonic()
                first_audio_sent = False

                async def emit_audio(audio_array: numpy.ndarray) -> None:
                    nonlocal first_audio_sent, pending_bytes

                    if audio_array.size == 0:
                        return

                    if self.cli_args.debug_wav:
                        debug_audio_arrays.append(audio_array.copy())

                    audio_bytes = (
                        audio_array.clip(-1.0, 1.0) * 32767
                    ).astype("int16").tobytes()
                    pending_bytes += audio_bytes

                    emit_len = (len(pending_bytes) // bytes_per_chunk) * bytes_per_chunk
                    if emit_len == 0:
                        return

                    bytes_to_emit = pending_bytes[:emit_len]
                    pending_bytes = pending_bytes[emit_len:]

                    for offset in range(0, len(bytes_to_emit), bytes_per_chunk):
                        chunk = bytes_to_emit[offset : offset + bytes_per_chunk]
                        if not first_audio_sent:
                            first_audio_sent = True
                            _LOGGER.info(
                                "Starting audio stream after %d ms",
                                int((time.monotonic() - synthesis_started_at) * 1000),
                            )
                        await self.write_event(
                            AudioChunk(
                                audio=chunk,
                                rate=sample_rate,
                                width=width,
                                channels=channels,
                            ).event(),
                        )

                async def flush_audio() -> None:
                    nonlocal pending_bytes

                    if not pending_bytes:
                        return

                    await self.write_event(
                        AudioChunk(
                            audio=pending_bytes,
                            rate=sample_rate,
                            width=width,
                            channels=channels,
                        ).event(),
                    )
                    pending_bytes = b""

                prefix_buffer = numpy.array([], dtype="float32")
                max_prefix_samples = int(sample_rate * PREFIX_MAX_DURATION)
                padding_samples = int(sample_rate * 0.05)
                prefix_trimmed = False
                leading_trimmed = False
                threshold = 0.0

                async def emit_after_leading_trim(audio_array: numpy.ndarray) -> None:
                    nonlocal leading_trimmed

                    if audio_array.size == 0:
                        return

                    if leading_trimmed:
                        await emit_audio(audio_array)
                        return

                    non_silent_indices = numpy.where(
                        numpy.abs(audio_array) > threshold
                    )[0]
                    if len(non_silent_indices) == 0:
                        return

                    first_non_silent = max(0, non_silent_indices[0] - padding_samples)
                    leading_trimmed = True
                    await emit_audio(audio_array[first_non_silent:])

                audio_generated = False
                for audio_chunk in audio_chunks:
                    audio_array = audio_chunk.detach().cpu().numpy()
                    audio_generated = True

                    if prefix_trimmed:
                        await emit_after_leading_trim(audio_array)
                        continue

                    prefix_buffer = numpy.concatenate((prefix_buffer, audio_array))
                    max_amplitude = numpy.abs(prefix_buffer).max()
                    threshold = max(max_amplitude * 0.01, 1e-5)
                    prefix_end = find_prefix_end(
                        prefix_buffer,
                        sample_rate=sample_rate,
                        threshold=threshold,
                    )

                    if prefix_end > 0:
                        _LOGGER.debug(
                            "Trimming prefix: %d samples (%.3fs)",
                            prefix_end,
                            prefix_end / sample_rate,
                        )
                        prefix_trimmed = True
                        await emit_after_leading_trim(prefix_buffer[prefix_end:])
                        prefix_buffer = numpy.array([], dtype="float32")
                        continue

                    if len(prefix_buffer) >= max_prefix_samples:
                        _LOGGER.warning(
                            "Could not detect prefix silence after %.3fs; streaming audio without prefix trim",
                            PREFIX_MAX_DURATION,
                        )
                        prefix_trimmed = True
                        await emit_after_leading_trim(prefix_buffer)
                        prefix_buffer = numpy.array([], dtype="float32")

                if not audio_generated:
                    if send_stop:
                        await self.write_event(AudioStop().event())
                    return True

                if not prefix_trimmed and prefix_buffer.size > 0:
                    max_amplitude = numpy.abs(prefix_buffer).max()
                    threshold = max(max_amplitude * 0.01, 1e-5)
                    prefix_end = find_prefix_end(
                        prefix_buffer,
                        sample_rate=sample_rate,
                        threshold=threshold,
                    )
                    if prefix_end > 0:
                        _LOGGER.debug(
                            "Trimming prefix: %d samples (%.3fs)",
                            prefix_end,
                            prefix_end / sample_rate,
                        )
                        await emit_after_leading_trim(prefix_buffer[prefix_end:])
                    else:
                        _LOGGER.warning(
                            "Could not detect prefix silence; emitting buffered audio without prefix trim"
                        )
                        await emit_after_leading_trim(prefix_buffer)

                await flush_audio()

                # Write debug WAV file if enabled
                if self.cli_args.debug_wav:
                    try:
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                        wav_filename = f"/output/debug_{voice_name}_{timestamp}.wav"
                        debug_audio = (
                            numpy.concatenate(debug_audio_arrays)
                            if debug_audio_arrays
                            else numpy.array([], dtype="float32")
                        )
                        debug_audio = (
                            debug_audio.clip(-1.0, 1.0) * 32767
                        ).astype("int16")
                        with wave.open(wav_filename, "wb") as wav_file:
                            wav_file.setnchannels(channels)
                            wav_file.setsampwidth(width)
                            wav_file.setframerate(sample_rate)
                            wav_file.writeframes(debug_audio.tobytes())
                        _LOGGER.info("Debug WAV file written: %s", wav_filename)
                    except Exception as e:
                        _LOGGER.warning("Failed to write debug WAV file: %s", e)

                if send_stop:
                    await self.write_event(AudioStop().event())

                _LOGGER.info("Synthesis complete")
            except Exception as e:
                _LOGGER.error("Error during synthesis: %s", e, exc_info=True)
                await self.write_event(
                    Error(text=str(e), code=e.__class__.__name__).event()
                )
                return True

        return True


async def main() -> None:
    """Main entry point."""
    global MODEL_CONFIG, MODEL_VARIANT

    parser = argparse.ArgumentParser(
        description="Wyoming Protocol TTS Server for Pocket-TTS"
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("WYOMING_HOST", "0.0.0.0"),
        help="Host to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        help=f"Default voice to use (default: {DEFAULT_VOICE})",
    )
    parser.add_argument(
        "--language",
        default=DEFAULT_LANGUAGE,
        help=f"Default language/model to use (default: {DEFAULT_LANGUAGE})",
    )
    parser.add_argument(
        "--variant",
        default=MODEL_VARIANT,
        help="Deprecated. Use --language for Pocket-TTS v2, or --config for a local YAML model config.",
    )
    parser.add_argument(
        "--config",
        default=MODEL_CONFIG,
        help="Local Pocket-TTS YAML model config. Overrides --language.",
    )
    parser.add_argument(
        "--uri",
        default=None,
        help="Server URI (e.g., tcp://0.0.0.0:10201). If not provided, constructed from --host and --port",
    )
    parser.add_argument(
        "--zeroconf",
        nargs="?",
        const="pocket-tts",
        help="Enable discovery over zeroconf with optional name (default: pocket-tts)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Reduce logging output",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Log DEBUG messages",
    )
    parser.add_argument(
        "--debug-wav",
        action="store_true",
        help="Write complete WAV file to /output/ on every response (default: from DEBUG_WAV env var)",
    )
    parser.add_argument(
        "--log-format",
        default=logging.BASIC_FORMAT,
        help="Format for log messages",
    )

    args = parser.parse_args()

    if "DEFAULT_VOICE" not in os.environ and args.voice == DEFAULT_VOICE:
        args.voice = default_voice_for_language(args.language)
    
    # Override debug_wav from environment if not explicitly set via command line
    # Check environment variable at runtime (not just at module load)
    debug_wav_env = os.environ.get("DEBUG_WAV", "").lower() in ("true", "1", "yes")
    if not args.debug_wav:
        args.debug_wav = debug_wav_env

    log_level = logging.DEBUG if args.debug else (logging.ERROR if args.quiet else logging.INFO)
    logging.basicConfig(level=log_level, format=args.log_format)
    if args.debug_wav:
        _LOGGER.info("Debug WAV mode enabled - WAV files will be written to /output/ on every response")
    _LOGGER.debug(args)

    if args.config:
        MODEL_CONFIG = args.config
        os.environ["MODEL_CONFIG"] = args.config
    if args.variant:
        MODEL_VARIANT = args.variant
        os.environ["MODEL_VARIANT"] = args.variant

    default_language = normalize_language(args.language)
    _LOGGER.info("Default language: %s", default_language)
    _LOGGER.info("Default voice: %s", args.voice)

    voices = [
        TtsVoice(
            name=voice_name,
            description=f"Pocket-TTS voice: {voice_name} ({get_voice_language(voice_name)})",
            attribution=Attribution(
                name="Kyutai Pocket-TTS",
                url="https://github.com/kyutai-labs/pocket-tts",
            ),
            installed=True,
            version=None,
            languages=[LANGUAGE_CODES.get(get_voice_language(voice_name), get_voice_language(voice_name))],
            speakers=None,
        )
        for voice_name in PREDEFINED_VOICES
    ]

    wyoming_info = Info(
        tts=[
            TtsProgram(
                name="pocket-tts",
                description="A fast, local, neural text to speech engine",
                attribution=Attribution(
                    name="Kyutai Pocket-TTS",
                    url="https://github.com/kyutai-labs/pocket-tts",
                ),
                installed=True,
                voices=sorted(voices, key=lambda v: v.name),
                version=None,
                supports_synthesize_streaming=True,
            )
        ],
    )

    if args.uri is None:
        args.uri = f"tcp://{args.host}:{args.port}"

    server = AsyncServer.from_uri(args.uri)

    zeroconf_name = args.zeroconf
    if not zeroconf_name:
        zeroconf_env = os.environ.get("ZEROCONF")
        if zeroconf_env:
            zeroconf_name = zeroconf_env if zeroconf_env != "true" else "pocket-tts"

    if zeroconf_name:
        if not isinstance(server, AsyncTcpServer):
            raise ValueError("Zeroconf requires tcp:// uri")

        from wyoming.zeroconf import HomeAssistantZeroconf
        import socket

        tcp_server: AsyncTcpServer = server
        zeroconf_host = tcp_server.host
        if zeroconf_host == "0.0.0.0" or not zeroconf_host:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                zeroconf_host = s.getsockname()[0]
                s.close()
            except Exception:
                zeroconf_host = "127.0.0.1"
        
        hass_zeroconf = HomeAssistantZeroconf(
            name=zeroconf_name, port=tcp_server.port, host=zeroconf_host
        )
        await hass_zeroconf.register_server()
        _LOGGER.debug("Zeroconf discovery enabled: name=%s, port=%d, host=%s", 
                     zeroconf_name, tcp_server.port, zeroconf_host)

    _LOGGER.info("Ready")
    _LOGGER.info("Available voices: %s", ", ".join(PREDEFINED_VOICES.keys()))
    await server.run(
        partial(
            PocketTTSEventHandler,
            wyoming_info,
            args,
        )
    )


def run():
    """Run the server."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _LOGGER.info("Server stopped")


if __name__ == "__main__":
    run()
