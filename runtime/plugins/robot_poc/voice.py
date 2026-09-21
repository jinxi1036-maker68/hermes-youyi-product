"""Local Step C voice ingress for the isolated Robot Channel POC.

This module does one narrow job: authenticated WAV bytes become a transcript,
then that transcript is submitted unchanged to the existing Robot text
channel.  It has no business vocabulary, intent classifier, Tool mapping or
reply generation.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import site
import tempfile
import threading
import time
from typing import Any, Callable, Protocol
import wave


VOICE_PROTOCOL_VERSION = "robot-poc-voice-v1"
MAX_AUDIO_BYTES = 8 * 1024 * 1024
ALLOWED_AUDIO_FORMATS = frozenset({"wav"})


class VoiceIngressRejected(ValueError):
    """A client-safe voice transport rejection."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class VoiceTranscript:
    text: str
    engine: str
    model: str
    language: str
    audio_duration_ms: float
    stt_latency_ms: float
    audio_sha256: str


class VoiceTranscriber(Protocol):
    def transcribe(self, audio: bytes, *, audio_format: str) -> VoiceTranscript: ...


class FasterWhisperTranscriber:
    """One local CPU STT model instance, loaded on the first POC voice turn.

    ``runtime_site_packages`` makes the STT package path explicit.  The
    isolated Step C dependency environment is therefore not installed into or
    coupled to Hermes itself.  Importing this class alone never downloads a
    model or makes a network request.
    """

    def __init__(
        self,
        *,
        model: str = "small",
        model_dir: Path | str | None = None,
        runtime_site_packages: Path | str | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        self.model = str(model or "small")
        self.model_dir = Path(model_dir).resolve() if model_dir else None
        self.runtime_site_packages = Path(runtime_site_packages).resolve() if runtime_site_packages else None
        self.device = str(device or "cpu")
        self.compute_type = str(compute_type or "int8")
        self._model: Any | None = None
        self._load_lock = threading.Lock()

    def warm(self) -> None:
        """Load the local STT engine before a teacher starts speaking.

        Warmup occurs only in the isolated Robot POC process.  It has no user
        text or audio and cannot enter Hermes, select a Tool, or affect a turn.
        """
        self._load_model()

    def transcribe(self, audio: bytes, *, audio_format: str) -> VoiceTranscript:
        if audio_format != "wav":
            raise VoiceIngressRejected("unsupported_audio_format", "目前仅接受 WAV 语音输入。")
        duration_ms = self._wav_duration_ms(audio)
        started = time.monotonic_ns()
        temp_path = ""
        try:
            with tempfile.NamedTemporaryFile(prefix="robot-poc-stt-", suffix=".wav", delete=False) as handle:
                handle.write(audio)
                temp_path = handle.name
            segments, info = self._load_model().transcribe(
                temp_path,
                language="zh",
                task="transcribe",
                beam_size=5,
                best_of=3,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            text = "".join(str(segment.text or "") for segment in segments).strip()
            return VoiceTranscript(
                text=text,
                engine="faster-whisper",
                model=self.model,
                language=str(getattr(info, "language", "zh") or "zh"),
                audio_duration_ms=duration_ms,
                stt_latency_ms=round((time.monotonic_ns() - started) / 1_000_000, 3),
                audio_sha256=hashlib.sha256(audio).hexdigest(),
            )
        except VoiceIngressRejected:
            raise
        except Exception as exc:
            raise VoiceIngressRejected("stt_failed", "本地语音转写失败，未进入业务处理。") from exc
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            if self.runtime_site_packages:
                site.addsitedir(str(self.runtime_site_packages))
            try:
                from faster_whisper import WhisperModel
            except ModuleNotFoundError as exc:
                raise VoiceIngressRejected("stt_runtime_unavailable", "本地 STT 运行时未安装。") from exc
            download_root = str(self.model_dir) if self.model_dir else None
            self._model = WhisperModel(
                self.model,
                device=self.device,
                compute_type=self.compute_type,
                download_root=download_root,
            )
        return self._model

    @staticmethod
    def _wav_duration_ms(audio: bytes) -> float:
        try:
            with wave.open(__import__("io").BytesIO(audio), "rb") as wav:
                frame_rate = int(wav.getframerate())
                frames = int(wav.getnframes())
                channels = int(wav.getnchannels())
                sample_width = int(wav.getsampwidth())
        except (wave.Error, EOFError):
            raise VoiceIngressRejected("invalid_audio", "语音不是有效 WAV 数据。") from None
        if frame_rate < 8_000 or frame_rate > 48_000 or channels not in {1, 2} or sample_width not in {1, 2, 3, 4}:
            raise VoiceIngressRejected("invalid_audio", "WAV 参数不在 Step C POC 支持范围内。")
        if frames <= 0:
            raise VoiceIngressRejected("invalid_audio", "语音不包含音频帧。")
        return round(frames * 1000.0 / frame_rate, 3)


async def _emit(progress_sink: Callable[[dict[str, Any]], Any] | None, event: dict[str, Any]) -> None:
    if progress_sink is None:
        return
    result = progress_sink(event)
    if asyncio.iscoroutine(result):
        await result


class VoiceIngress:
    """Bridge a locally transcribed voice turn into RobotPocAdapter."""

    def __init__(self, *, adapter: Any, transcriber: VoiceTranscriber) -> None:
        self.adapter = adapter
        self.transcriber = transcriber

    async def submit(
        self,
        envelope: dict[str, Any],
        *,
        progress_sink: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        trusted = self.adapter.validate_voice_envelope(envelope)
        audio_format = str(envelope.get("audio_format") or "").strip().lower()
        if audio_format not in ALLOWED_AUDIO_FORMATS:
            raise VoiceIngressRejected("unsupported_audio_format", "目前仅接受 WAV 语音输入。")
        try:
            audio = base64.b64decode(str(envelope.get("audio_base64") or ""), validate=True)
        except (ValueError, TypeError):
            raise VoiceIngressRejected("invalid_audio", "语音数据不是有效 Base64。") from None
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            raise VoiceIngressRejected("invalid_audio", "语音数据为空或超过 POC 限制。")
        await _emit(progress_sink, {"kind": "audio_received", "source": "voice_transport"})
        transcript = await asyncio.to_thread(self.transcriber.transcribe, audio, audio_format=audio_format)
        text = str(transcript.text or "").strip()
        if not text:
            raise VoiceIngressRejected("speech_not_recognized", "没有取得可用转写，未进入业务处理。")
        await _emit(progress_sink, {"kind": "transcription_completed", "source": "stt"})
        text_envelope = dict(trusted)
        text_envelope.update({"protocol_version": "robot-poc-v0", "text": text})
        metadata = {
            "input_mode": "voice",
            "stt_engine": str(transcript.engine or "")[:80],
            "stt_model": str(transcript.model or "")[:120],
            "stt_language": str(transcript.language or "")[:32],
            "stt_latency_ms": round(float(transcript.stt_latency_ms or 0.0), 3),
            "audio_duration_ms": round(float(transcript.audio_duration_ms or 0.0), 3),
            "audio_sha256": str(transcript.audio_sha256 or hashlib.sha256(audio).hexdigest())[:64],
        }
        return await self.adapter.submit_text(
            text_envelope,
            ingress_metadata=metadata,
            progress_sink=progress_sink,
        )
