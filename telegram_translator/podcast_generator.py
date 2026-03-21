"""Voicebox-based podcast audio generation."""

import hashlib
import json
import logging
import re
import shutil
import struct
import wave
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def split_script(text: str, max_chars: int = 500) -> list[str]:
    """Split a podcast script into segments at sentence boundaries.

    Args:
        text: The full podcast script.
        max_chars: Maximum characters per segment.

    Returns:
        List of text segments.
    """
    if len(text) <= max_chars:
        return [text]

    sentences = re.split(r"(?<=[.!?])\s+", text)

    segments: list[str] = []
    current = ""

    for sentence in sentences:
        if len(sentence) > max_chars:
            if current.strip():
                segments.append(current.strip())
                current = ""
            words = sentence.split()
            chunk = ""
            for word in words:
                while len(word) > max_chars:
                    if chunk.strip():
                        segments.append(chunk.strip())
                        chunk = ""
                    segments.append(word[:max_chars])
                    word = word[max_chars:]
                if chunk and len(chunk) + len(word) + 1 > max_chars:
                    segments.append(chunk.strip())
                    chunk = word
                else:
                    chunk = f"{chunk} {word}" if chunk else word
            if chunk:
                current = chunk
            continue

        if len(current) + len(sentence) + 1 > max_chars:
            if current.strip():
                segments.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence

    if current.strip():
        segments.append(current.strip())

    return segments


def split_script_by_topics(
    text: str,
    max_chars: int = 500,
) -> tuple[list[str], set[int]]:
    """Split a plain-text script into TTS segments, tracking topic boundaries.

    Topic boundaries are detected by lines starting with ``**`` (Markdown
    bold headers).  Used as a legacy fallback for scripts stored before
    structured output was introduced.

    Args:
        text: The full podcast script.
        max_chars: Max chars per TTS segment.

    Returns:
        A tuple of (segments, topic_boundary_indices).
    """
    topic_pattern = re.compile(r"(?=^\*\*)", re.MULTILINE)
    topic_blocks = topic_pattern.split(text)
    topic_blocks = [b.strip() for b in topic_blocks if b.strip()]

    all_segments: list[str] = []
    topic_boundaries: set[int] = set()

    for block in topic_blocks:
        if all_segments:
            topic_boundaries.add(len(all_segments))
        chunks = split_script(block, max_chars=max_chars)
        all_segments.extend(chunks)

    return all_segments, topic_boundaries


def parse_structured_sections(
    script_json: str,
    max_chars: int = 500,
) -> tuple[list[str], set[int]]:
    """Parse a structured JSON script into TTS segments with topic boundaries.

    Args:
        script_json: JSON string with ``{"sections": [...]}``.
        max_chars: Max chars per TTS segment.

    Returns:
        A tuple of (segments, topic_boundary_indices).
    """
    data = json.loads(script_json)
    sections = data["sections"]

    all_segments: list[str] = []
    topic_boundaries: set[int] = set()

    for section in sections:
        text = section.get("text", "").strip()
        if not text:
            continue
        if section.get("topic"):
            topic_boundaries.add(len(all_segments))
        chunks = split_script(text, max_chars=max_chars)
        all_segments.extend(chunks)

    return all_segments, topic_boundaries


def sections_to_readable(script_json: str) -> str:
    """Convert structured JSON sections to human-readable text.

    Args:
        script_json: JSON string with ``{"sections": [...]}``.

    Returns:
        Readable text with ``[Topic Name]`` headers.
    """
    data = json.loads(script_json)
    parts: list[str] = []
    for section in data["sections"]:
        topic = section.get("topic")
        text = section.get("text", "").strip()
        if topic:
            parts.append(f"[{topic}]")
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _load_asset_wav(
    asset_path: Path,
    target_rate: int = 24000,
) -> list[int]:
    """Load a WAV asset, downmix to mono and resample to target rate.

    Args:
        asset_path: Path to the WAV file.
        target_rate: Desired sample rate.

    Returns:
        List of 16-bit signed PCM sample values (mono).
    """
    with wave.open(str(asset_path), "rb") as wf:
        n_channels = wf.getnchannels()
        src_rate = wf.getframerate()
        samp_width = wf.getsampwidth()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if samp_width != 2:
        raise ValueError(f"Unsupported sample width: {samp_width}")

    fmt = f"<{n_frames * n_channels}h"
    all_samples = list(struct.unpack(fmt, raw))

    # Downmix to mono
    if n_channels == 2:
        mono = [
            (all_samples[i] + all_samples[i + 1]) // 2
            for i in range(0, len(all_samples), 2)
        ]
    else:
        mono = all_samples

    # Resample if needed
    if src_rate != target_rate:
        ratio = src_rate / target_rate
        new_len = int(len(mono) / ratio)
        resampled = []
        for i in range(new_len):
            src_pos = i * ratio
            idx = int(src_pos)
            frac = src_pos - idx
            if idx + 1 < len(mono):
                val = mono[idx] * (1 - frac) + mono[idx + 1] * frac
            else:
                val = mono[idx] if idx < len(mono) else 0
            resampled.append(int(val))
        mono = resampled

    return mono


def _load_audio_asset(
    asset_path: Path,
    target_rate: int = 24000,
) -> list[int]:
    """Load a WAV or MP3 asset and return mono samples at target rate.

    Args:
        asset_path: Path to the audio file (.wav or .mp3).
        target_rate: Desired sample rate.

    Returns:
        List of 16-bit signed PCM sample values (mono).
    """
    if asset_path.suffix.lower() == ".mp3":
        from pydub import AudioSegment

        audio = AudioSegment.from_mp3(str(asset_path))
        audio = audio.set_channels(1).set_frame_rate(target_rate).set_sample_width(2)
        raw = audio.raw_data
        return list(struct.unpack(f"<{len(raw) // 2}h", raw))

    return _load_asset_wav(asset_path, target_rate=target_rate)


class PodcastGenerator:
    """Generate podcast audio via Voicebox API."""

    def __init__(
        self,
        config: dict,
        tts_cache_dir: Path | None = None,
        no_cache: bool = False,
    ):
        """Initialize the podcast generator.

        Args:
            config: Resolved podcast config dict with keys: voicebox_url,
                voice_profile, language, output_dir, pause_between_segments_ms,
                audio (dict with asset paths and mixing params), name.
            tts_cache_dir: Directory for TTS segment cache files.
            no_cache: If True, bypass TTS cache entirely.
        """
        self.tts_cache_dir = tts_cache_dir if not no_cache else None
        self.podcast_name = config.get("name", "_default")
        self.voicebox_url = config.get(
            "voicebox_url", "http://localhost:17493"
        )
        self.voice_profile_name = config.get("voice_profile", "default")
        self.language = config.get("language", "en")
        self.output_dir = Path(config.get("output_dir", "./podcasts"))
        self.pause_ms = config.get("pause_between_segments_ms", 800)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Audio asset paths and mixing parameters
        audio = config.get("audio", {})
        default_assets = Path(__file__).resolve().parent.parent / "podcasts" / "assets"
        self.whoosh_path = Path(
            audio.get("whoosh", default_assets / "whoosh.wav")
        )
        self.intro_bed_path = Path(
            audio.get("intro_bed", default_assets / "news_bed.wav")
        )
        self.background_bed_path = Path(
            audio.get("background_bed", default_assets / "background_bed.mp3")
        )
        self.lead_in_seconds = float(audio.get("lead_in_seconds", 4.0))
        self.intro_fade_seconds = float(audio.get("intro_fade_seconds", 2.0))
        self.intro_bed_volume = float(audio.get("intro_bed_volume", 0.7))
        self.background_bed_volume = float(
            audio.get("background_bed_volume", 0.08)
        )
        self.background_fade_seconds = float(
            audio.get("background_fade_seconds", 3.0)
        )

        self._profile_id: Optional[str] = None

    # -- Audio asset loading methods --

    def _load_whoosh_frames(self, sample_rate: int) -> bytes:
        """Load the whoosh asset and wrap with silence padding.

        Args:
            sample_rate: Target sample rate.

        Returns:
            Raw PCM bytes (mono 16-bit).
        """
        if not self.whoosh_path.exists():
            n = int(sample_rate * 0.5)
            return b"\x00\x00" * n

        whoosh_samples = _load_audio_asset(
            self.whoosh_path, target_rate=sample_rate
        )
        pause_n = int(sample_rate * self.pause_ms / 2 / 1000)
        silence = [0] * pause_n
        all_samples = silence + whoosh_samples + silence
        return struct.pack(f"<{len(all_samples)}h", *all_samples)

    def _load_intro_bed(
        self, target_samples: int, sample_rate: int
    ) -> list[int]:
        """Load the intro bed asset, apply volume and fade-out.

        Args:
            target_samples: Total podcast length in samples.
            sample_rate: Target sample rate.

        Returns:
            List of 16-bit sample values, padded to target length.
        """
        if not self.intro_bed_path.exists():
            return [0] * target_samples

        bed_raw = _load_audio_asset(
            self.intro_bed_path, target_rate=sample_rate
        )

        fade_samples = int(sample_rate * self.intro_fade_seconds)
        for i in range(len(bed_raw)):
            fade = 1.0
            remaining = len(bed_raw) - i
            if remaining < fade_samples:
                fade = remaining / fade_samples
            bed_raw[i] = int(bed_raw[i] * self.intro_bed_volume * fade)

        if len(bed_raw) < target_samples:
            bed_raw.extend([0] * (target_samples - len(bed_raw)))
        else:
            bed_raw = bed_raw[:target_samples]

        return bed_raw

    def _load_background_bed(
        self, target_samples: int, sample_rate: int
    ) -> list[int]:
        """Load and loop the background music bed.

        Args:
            target_samples: Total podcast length in samples.
            sample_rate: Target sample rate.

        Returns:
            List of 16-bit sample values, same length as the podcast.
        """
        if not self.background_bed_path.exists():
            return [0] * target_samples

        clip = _load_audio_asset(
            self.background_bed_path, target_rate=sample_rate
        )
        if not clip:
            return [0] * target_samples

        # Loop with crossfade
        crossfade_samples = int(sample_rate * 2.0)
        looped: list[int] = []

        while len(looped) < target_samples:
            if looped and crossfade_samples > 0:
                cf_len = min(crossfade_samples, len(looped), len(clip))
                for j in range(cf_len):
                    fade_out = (cf_len - j) / cf_len
                    fade_in = j / cf_len
                    idx = len(looped) - cf_len + j
                    looped[idx] = int(
                        looped[idx] * fade_out + clip[j] * fade_in
                    )
                looped.extend(clip[cf_len:])
            else:
                looped.extend(clip)

        looped = looped[:target_samples]

        # Volume and fade-in/out
        fade_samples = int(sample_rate * self.background_fade_seconds)
        for i in range(len(looped)):
            fade = 1.0
            if i < fade_samples:
                fade = i / fade_samples
            elif i > len(looped) - fade_samples:
                fade = (len(looped) - i) / fade_samples
            looped[i] = int(looped[i] * self.background_bed_volume * fade)

        return looped

    # -- Assembly --

    def assemble_podcast(
        self,
        wav_paths: list[Path],
        output_path: Path,
        topic_boundaries: set[int] | None = None,
    ) -> Path:
        """Concatenate voice segments with transitions, beds, and mixing.

        Args:
            wav_paths: Ordered list of voice WAV file paths.
            output_path: Where to write the final podcast WAV.
            topic_boundaries: Segment indices where a new topic starts
                (whoosh inserted before these). Whoosh everywhere if None.

        Returns:
            The output path.
        """
        if not wav_paths:
            raise ValueError("No WAV files to concatenate")

        if len(wav_paths) == 1:
            import shutil
            shutil.copy2(wav_paths[0], output_path)
            return output_path

        with wave.open(str(wav_paths[0]), "rb") as first:
            params = first.getparams()
            sample_rate = first.getframerate()

        transition_frames = self._load_whoosh_frames(sample_rate)

        brief_silence_samples = int(sample_rate * 150 / 1000)
        brief_silence = b"\x00\x00" * brief_silence_samples * params.nchannels

        # Assemble voice frames
        voice_frames = bytearray()
        for i, path in enumerate(wav_paths):
            with wave.open(str(path), "rb") as wf:
                voice_frames.extend(wf.readframes(wf.getnframes()))

            if i >= len(wav_paths) - 1:
                continue

            next_idx = i + 1
            if topic_boundaries is None or next_idx in topic_boundaries:
                voice_frames.extend(transition_frames)
            else:
                voice_frames.extend(brief_silence)

        # Prepend lead-in silence for the intro bed
        lead_in_samples = int(sample_rate * self.lead_in_seconds)
        lead_in_bytes = b"\x00\x00" * lead_in_samples * params.nchannels
        voice_frames = bytearray(lead_in_bytes) + voice_frames

        # Load and mix audio beds
        total_samples = len(voice_frames) // (
            params.sampwidth * params.nchannels
        )
        intro_bed = self._load_intro_bed(total_samples, sample_rate)
        bg_bed = self._load_background_bed(total_samples, sample_rate)

        voice_ints = struct.unpack(
            f"<{total_samples}h", bytes(voice_frames)
        )
        mixed = []
        for v, ib, bb in zip(voice_ints, intro_bed, bg_bed):
            s = max(-32767, min(32767, v + ib + bb))
            mixed.append(s)

        mixed_bytes = struct.pack(f"<{len(mixed)}h", *mixed)

        with wave.open(str(output_path), "wb") as out:
            out.setparams(params)
            out.writeframes(mixed_bytes)

        return output_path

    # -- Voicebox API --

    async def _get_profile_id(self) -> str:
        """Look up the voice profile ID by name.

        Returns:
            The profile ID string.

        Raises:
            RuntimeError: If the profile is not found or Voicebox is
                unreachable.
        """
        if self._profile_id:
            return self._profile_id

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{self.voicebox_url}/profiles"
                )
                response.raise_for_status()
                profiles = response.json()
        except httpx.HTTPError:
            raise RuntimeError(
                f"Cannot reach Voicebox at {self.voicebox_url}. "
                "Is the backend running?"
            )

        for profile in profiles:
            if profile.get("name", "").lower() == self.voice_profile_name.lower():
                self._profile_id = profile["id"]
                logger.info(
                    "Resolved voice profile '%s' -> %s",
                    self.voice_profile_name,
                    self._profile_id,
                )
                return self._profile_id

        available = [p.get("name", "?") for p in profiles]
        raise RuntimeError(
            f"Voice profile '{self.voice_profile_name}' not found. "
            f"Available profiles: {available}"
        )

    def _tts_cache_path(self, text: str) -> Path | None:
        """Compute the TTS cache file path for a text segment.

        Args:
            text: Segment text.

        Returns:
            Cache file path, or None if caching is disabled.
        """
        if not self.tts_cache_dir:
            return None
        payload = f"{text}:{self.voice_profile_name}"
        h = hashlib.sha256(payload.encode()).hexdigest()
        return self.tts_cache_dir / f"{h}.wav"

    async def generate_segment(
        self,
        text: str,
        output_path: Path,
    ) -> Path:
        """Generate audio for a single text segment via Voicebox.

        Args:
            text: Text to speak (must be <= 5000 chars).
            output_path: Where to save the WAV file.

        Returns:
            Path to the generated WAV file.

        Raises:
            RuntimeError: If generation fails.
        """
        # Check TTS cache
        cached = self._tts_cache_path(text)
        if cached and cached.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached, output_path)
            logger.info("TTS cache hit for segment")
            return output_path

        profile_id = await self._get_profile_id()

        payload = {
            "text": text,
            "profile_id": profile_id,
            "language": self.language,
        }

        try:
            async with httpx.AsyncClient(timeout=900) as client:
                response = await client.post(
                    f"{self.voicebox_url}/generate",
                    json=payload,
                )
                response.raise_for_status()
                result = response.json()
                generation_id = result["id"]
                duration = result.get("duration", 0)
                logger.info(
                    "Generation %s complete (%.1fs audio)",
                    generation_id,
                    duration,
                )

                audio_response = await client.get(
                    f"{self.voicebox_url}/audio/{generation_id}",
                )
                audio_response.raise_for_status()

                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(audio_response.content)
                logger.info(
                    "Saved audio segment: %s (%d bytes)",
                    output_path,
                    len(audio_response.content),
                )

                # Save to TTS cache
                if cached:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(output_path, cached)

                return output_path

        except httpx.HTTPError:
            logger.error("Voicebox request failed", exc_info=True)
            raise RuntimeError(
                "Voicebox generation failed for segment"
            )

    async def generate_podcast(
        self,
        script: str,
        date: str,
    ) -> Path:
        """Generate a full podcast from a script.

        Accepts either structured JSON (from structured output) or legacy
        plain-text scripts with ``**`` markdown headers.

        Args:
            script: The podcast script (JSON or plain text).
            date: Date string for the output filename.

        Returns:
            Path to the final podcast WAV file.
        """
        # Detect format and parse accordingly
        script_path = self.output_dir / f"{self.podcast_name}_{date}.txt"
        is_structured = script.lstrip().startswith("{")

        if is_structured:
            readable = sections_to_readable(script)
            script_path.write_text(readable, encoding="utf-8")
            segments, topic_boundaries = parse_structured_sections(script)
        else:
            script_path.write_text(script, encoding="utf-8")
            segments, topic_boundaries = split_script_by_topics(script)

        logger.info("Script saved: %s", script_path)
        logger.info(
            "Split podcast script into %d segments (%d topic boundaries)",
            len(segments),
            len(topic_boundaries),
        )

        segment_dir = self.output_dir / f".segments_{date}"
        segment_dir.mkdir(parents=True, exist_ok=True)

        segment_paths = []
        for i, segment_text in enumerate(segments):
            segment_path = segment_dir / f"segment_{i:03d}.wav"
            await self.generate_segment(segment_text, segment_path)
            segment_paths.append(segment_path)
            logger.info(
                "Generated segment %d/%d", i + 1, len(segments)
            )

        # Output filename includes podcast name
        filename = f"{self.podcast_name}_{date}.wav"
        output_path = self.output_dir / filename
        self.assemble_podcast(
            segment_paths, output_path, topic_boundaries
        )

        for p in segment_paths:
            p.unlink(missing_ok=True)
        segment_dir.rmdir()

        logger.info("Podcast generated: %s", output_path)
        return output_path
