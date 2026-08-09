#!/usr/bin/env python3
"""Deepgram transcription (nova-3 + diarization), stdlib only.

Kept dependency-free on purpose: this module has to run inside a py2app
bundle, so it uses urllib rather than requests.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import config

DEEPGRAM_ENDPOINT = "https://api.deepgram.com/v1/listen"

# Retry only on transient failures; see transcribe().
BACKOFFS = [5, 15, 30, 60]


class DeepgramAuthError(RuntimeError):
    """Key missing, wrong, or out of credit — the user has to fix it."""


def log(msg: str) -> None:
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}\n"
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 — bundled stdio can be ascii-only
        pass
    try:
        config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(config.LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _url(language: str) -> str:
    params = {
        "model": "nova-3",
        "language": language,
        "diarize": "true",
        "punctuate": "true",
        "paragraphs": "true",
        "utterances": "true",
        "smart_format": "true",
    }
    return f"{DEEPGRAM_ENDPOINT}?{urllib.parse.urlencode(params)}"


def transcribe(audio_path: Path) -> dict:
    cfg = config.load()
    key = cfg["deepgram_api_key"].strip()
    if not key:
        raise DeepgramAuthError(
            "Не задан ключ Deepgram — открой настройки в меню приложения."
        )

    audio_bytes = audio_path.read_bytes()
    url = _url(cfg["language"])
    content_type = "audio/m4a" if audio_path.suffix == ".m4a" else "audio/wav"

    last_err: Exception | None = None
    for attempt in range(len(BACKOFFS) + 1):
        req = urllib.request.Request(
            url,
            data=audio_bytes,
            headers={"Authorization": f"Token {key}", "Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=900) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            # 401 = bad key, 402 = out of credit. Retrying can't help either.
            if e.code in (401, 402, 403):
                raise DeepgramAuthError(
                    f"Deepgram отклонил ключ (HTTP {e.code}). {body}"
                ) from e
            if e.code < 500:
                raise RuntimeError(f"Deepgram HTTP {e.code}: {body}") from e
            last_err = e
        except (urllib.error.URLError, OSError) as e:
            # DNS, dropped connection, timeout — worth another try.
            last_err = e

        if attempt < len(BACKOFFS):
            wait = BACKOFFS[attempt]
            log(f"  попытка {attempt + 1} не удалась ({type(last_err).__name__}); "
                f"повтор через {wait} с")
            time.sleep(wait)

    raise last_err if last_err else RuntimeError("transcription failed")


def format_transcript(response: dict) -> str:
    """Deepgram JSON → `Speaker N | MM:SS` blocks."""
    results = response.get("results") or {}
    utterances = results.get("utterances") or []
    if utterances:
        lines: list[str] = []
        for u in utterances:
            text = (u.get("transcript") or "").strip()
            if not text:
                continue
            mm, ss = divmod(int(u.get("start", 0)), 60)
            lines.append(f"Speaker {u.get('speaker', 0)} | {mm:02d}:{ss:02d}")
            lines.append(text)
            lines.append("")
        return "\n".join(lines).strip()

    try:
        return results["channels"][0]["alternatives"][0]["transcript"].strip()
    except (KeyError, IndexError):
        return ""
