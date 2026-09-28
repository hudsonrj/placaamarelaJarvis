"""Fala e escuta no computador, no Bluetooth e nos aparelhos ligados nele."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import wave

import numpy as np

_SKIP = (
    "mapeador",
    "mapper",
    "primario",
    "primary",
    "mixagem",
    "stereo mix",
    "loopback",
    "per-process",
)
_BT = (
    "bluetooth",
    "headset",
    "soundcore",
    "airpod",
    "buds",
    "fone",
    "headphone",
    "jabra",
    "sony wh",
)
_PC = (
    "realtek",
    "smart sound",
    "microfone",
    "microphone",
    "alto-falante",
    "altofalante",
    "speaker",
    "internal",
)


def _sd():
    import sounddevice as sd

    return sd


def _fold(name: str) -> str:
    text = (name or "").lower()
    text = text.replace("á", "a").replace("ã", "a").replace("â", "a")
    text = text.replace("é", "e").replace("ê", "e")
    text = text.replace("í", "i")
    text = text.replace("ó", "o").replace("õ", "o").replace("ô", "o")
    text = text.replace("ú", "u")
    text = text.replace("ç", "c")
    return text


def _family(name: str) -> str | None:
    text = _fold(name).split("(")[0]
    text = re.sub(r"[^a-z0-9]+", "", text)
    if not text or any(skip in _fold(name) for skip in _SKIP):
        return None
    return text


def _kind(name: str) -> str:
    text = _fold(name)
    if any(token in text for token in _BT):
        return "bluetooth"
    if any(token in text for token in _PC):
        return "pc"
    return "outro"


def _ranked(kind: str) -> list[dict]:
    sd = _sd()
    hostapis = sd.query_hostapis()
    seen: dict[str, dict] = {}
    order = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2, "Windows WDM-KS": 3}

    for index, device in enumerate(sd.query_devices()):
        api = hostapis[device["hostapi"]]["name"]
        rank = order.get(api, 9)
        if rank > 2:
            continue
        family = _family(str(device.get("name") or ""))
        if family is None:
            continue
        channels = int(device.get("max_input_channels" if kind == "in" else "max_output_channels") or 0)
        if channels <= 0:
            continue
        # Saida hands-free e telefone estreito: fica de fora quando ha um fone estéreo.
        current = seen.get(family)
        if current is not None and current["rank"] <= rank:
            continue
        seen[family] = {
            "index": index,
            "name": str(device.get("name") or f"dispositivo {index}"),
            "kind": _kind(str(device.get("name") or "")),
            "rank": rank,
            "channels": channels,
            "rate": int(device.get("default_samplerate") or 44100),
        }
    devices = list(seen.values())
    if kind == "out":
        def hands_free(item: dict) -> bool:
            folded = _fold(item["name"])
            return "hands-free" in folded or "handsfree" in folded or "bthhf" in folded

        wider = [item for item in devices if not hands_free(item)]
        if wider:
            devices = wider
    if kind == "in":
        devices.sort(key=lambda item: (0 if item["kind"] == "bluetooth" else 1 if item["kind"] == "pc" else 2, item["name"]))
    else:
        devices.sort(key=lambda item: (item["kind"], item["name"]))
    for item in devices:
        item.pop("rank", None)
    return devices


def describe() -> dict:
    inputs = _ranked("in")
    outputs = _ranked("out")
    return {"inputs": inputs, "outputs": outputs}


def _resample(data: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or len(data) < 2:
        return data.astype(np.float32)
    count = max(2, int(len(data) * dst / src))
    grid = np.linspace(0, len(data) - 1, count)
    return np.interp(grid, np.arange(len(data)), data.astype(np.float32)).astype(np.float32)


def _play(device: dict, samples: np.ndarray, src_rate: int) -> None:
    sd = _sd()
    rate = int(device["rate"] or src_rate)
    audio = _resample(samples, src_rate, rate)
    channels = 2 if int(device["channels"]) >= 2 else 1
    buffer = np.column_stack((audio, audio)) if channels == 2 else audio
    with sd.OutputStream(
        device=int(device["index"]),
        samplerate=rate,
        channels=channels,
        dtype="float32",
        latency="high",
    ) as stream:
        stream.write(buffer.astype(np.float32))


def _tone(ms: int = 70, hz: float = 660.0, rate: int = 44100) -> np.ndarray:
    count = int(rate * ms / 1000)
    t = np.arange(count, dtype=np.float32) / rate
    env = np.linspace(0.0, 1.0, max(1, count // 5))
    envelope = np.ones(count, dtype=np.float32)
    envelope[: env.size] = env
    envelope[-env.size :] = env[::-1][: envelope[-env.size :].size]
    return (0.12 * np.sin(2 * np.pi * hz * t) * envelope).astype(np.float32)


def _sapi_wav(text: str, path: str) -> bool:
    safe = path.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$pt = $s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo } |"
        " Where-Object { $_.Culture.Name -like 'pt*' } | Select-Object -First 1;"
        "if ($pt) { $s.SelectVoice($pt.Name) };"
        "$s.Rate = -1;"
        f"$s.SetOutputToWaveFile('{safe}');"
        "$s.Speak([Console]::In.ReadToEnd());"
        "$s.Dispose()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            input=text[:900].encode("utf-8"),
            timeout=90,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return os.path.isfile(path) and os.path.getsize(path) > 44
    except (OSError, subprocess.TimeoutExpired):
        return False


def _load_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as handle:
        rate = handle.getframerate() or 22050
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    if width != 2 or not raw:
        return np.zeros(1, dtype=np.float32), rate
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def play_samples(samples: np.ndarray, rate: int, outputs: list[dict] | None = None) -> list[str]:
    devices = outputs if outputs is not None else _ranked("out")
    if not devices:
        return []
    used: list[str] = []
    errors: list[str] = []

    def run(device: dict) -> None:
        try:
            _play(device, samples, rate)
            used.append(device["name"])
        except Exception as exc:
            errors.append(f"{device['name']}: {exc}")

    threads = [threading.Thread(target=run, args=(device,), daemon=True) for device in devices]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    if errors and not used:
        print("audio sem saida:", "; ".join(errors[:4]))
    return used


def blip() -> list[str]:
    return play_samples(_tone(), 44100)


def speak(text: str) -> list[str]:
    spoken = (text or "").strip()
    if not spoken:
        return []
    handle, path = tempfile.mkstemp(suffix=".wav")
    os.close(handle)
    try:
        if not _sapi_wav(spoken, path):
            print("audio: voz pt-BR nao gerou wav")
            return []
        samples, rate = _load_wav(path)
        return play_samples(samples, rate)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    values = block.astype(np.float32)
    return float(np.sqrt(np.mean(values * values)))


def _to_text(samples: np.ndarray, rate: int) -> str:
    audio16 = _resample(samples.astype(np.float32), rate, 16000)
    pcm = np.clip(audio16 * 32768.0, -32768, 32767).astype(np.int16)
    if pcm.size < 1600:
        return ""
    try:
        from speech_recognition import AudioData, Recognizer, RequestError, UnknownValueError
    except Exception as exc:
        print("audio: speech_recognition indisponivel:", exc)
        return _sapi_listen(6)
    try:
        heard = Recognizer().recognize_google(AudioData(pcm.tobytes(), 16000, 2), language="pt-BR")
        return (heard or "").strip()
    except UnknownValueError:
        return ""
    except RequestError as exc:
        print("audio: google stt falhou:", exc)
        return _sapi_listen(6)
    except Exception as exc:
        print("audio: stt:", exc)
        return ""


def _sapi_listen(seconds: int) -> str:
    script = f"""
Add-Type -AssemblyName System.Speech
try {{ $e = New-Object System.Speech.Recognition.SpeechRecognitionEngine((New-Object Globalization.CultureInfo 'pt-BR')) }}
catch {{ try {{ $e = New-Object System.Speech.Recognition.SpeechRecognitionEngine }} catch {{ exit 0 }} }}
try {{ $e.SetInputToDefaultAudioDevice() }} catch {{ exit 0 }}
$e.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$e.InitialSilenceTimeout = [TimeSpan]::FromSeconds(2)
$e.EndSilenceTimeout = [TimeSpan]::FromSeconds(0.8)
$r = $e.Recognize([TimeSpan]::FromSeconds({int(seconds)}))
if ($r -and $r.Text) {{ Write-Output $r.Text }}
"""
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=seconds + 12,
            check=False,
        )
        text = (done.stdout or b"").decode("utf-8", "replace").strip()
        return text.splitlines()[-1].strip() if text else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _record(device: dict, seconds: float) -> tuple[np.ndarray, int, bool]:
    sd = _sd()
    rate = int(device["rate"] or 16000)
    if rate < 8000:
        rate = 16000
    block = max(1, int(rate * 0.1))
    frames: list[np.ndarray] = []
    voiced = False
    silence = 0.0
    elapsed = 0.0
    with sd.InputStream(
        device=int(device["index"]),
        samplerate=rate,
        channels=1,
        dtype="int16",
        blocksize=block,
        latency="high",
    ) as stream:
        while elapsed < seconds:
            data, overflowed = stream.read(block)
            del overflowed
            chunk = np.asarray(data).reshape(-1)
            frames.append(chunk.copy())
            elapsed += block / rate
            if _rms(chunk) > 420:
                voiced = True
                silence = 0.0
            elif voiced:
                silence += block / rate
                if silence >= 0.85 and elapsed > 0.4:
                    break
    if not frames:
        return np.zeros(0, dtype=np.int16), rate, False
    return np.concatenate(frames), rate, voiced


_capture_lock = threading.Lock()


def _capture(device: dict, stop, limit: float, sink: list) -> None:
    sd = _sd()
    rate = int(device["rate"] or 16000)
    if rate < 8000:
        rate = 16000
    block = max(1, int(rate * 0.1))
    frames: list[np.ndarray] = []
    elapsed = 0.0
    try:
        with sd.InputStream(
            device=int(device["index"]),
            samplerate=rate,
            channels=1,
            dtype="int16",
            blocksize=block,
            latency="high",
        ) as stream:
            while elapsed < limit and not stop.is_set():
                data, overflowed = stream.read(block)
                del overflowed
                frames.append(np.asarray(data).reshape(-1).copy())
                elapsed += block / rate
    except Exception as exc:
        print(f"audio gravacao {device['name']}: {exc}")
        return
    if not frames:
        return
    samples = np.concatenate(frames)
    item = (_rms(samples.astype(np.float32)), samples, rate, device["name"])
    with _capture_lock:
        sink.append(item)


def record_until(stop, limit: float = 30.0) -> tuple[np.ndarray, int]:
    devices = _ranked("in")[:3]
    if not devices:
        return np.zeros(0, dtype=np.int16), 16000
    try:
        blip()
    except Exception as exc:
        print("audio blip:", exc)
    sink: list = []
    threads = [
        threading.Thread(target=_capture, args=(device, stop, limit, sink), daemon=True)
        for device in devices
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=limit + 5)
    if not sink:
        return np.zeros(0, dtype=np.int16), 16000
    sink.sort(key=lambda item: item[0], reverse=True)
    rms, samples, rate, name = sink[0]
    print("audio mic:", name, "rms", round(rms, 1))
    return samples, rate


def _sapi_wave(samples: np.ndarray, rate: int) -> str:
    audio = _resample(samples.astype(np.float32), rate, 16000)
    if np.max(np.abs(audio)) > 2:
        audio = audio / 32768.0
    pcm = np.clip(audio * 32768.0, -32768, 32767).astype(np.int16)
    if pcm.size < 1600:
        return ""
    handle, path = tempfile.mkstemp(suffix=".wav")
    os.close(handle)
    safe = path.replace("'", "''")
    script = f"""
Add-Type -AssemblyName System.Speech
try {{ $e = New-Object System.Speech.Recognition.SpeechRecognitionEngine((New-Object Globalization.CultureInfo 'pt-BR')) }}
catch {{ try {{ $e = New-Object System.Speech.Recognition.SpeechRecognitionEngine }} catch {{ exit 0 }} }}
try {{
  $e.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
  $e.SetInputToWaveFile('{safe}')
}} catch {{ exit 0 }}
$e.InitialSilenceTimeout = [TimeSpan]::FromSeconds(4)
$e.EndSilenceTimeout = [TimeSpan]::FromSeconds(1.5)
$r = $e.Recognize()
if ($r -and $r.Text) {{ Write-Output $r.Text }}
"""
    try:
        with wave.open(path, "wb") as handle_wav:
            handle_wav.setnchannels(1)
            handle_wav.setsampwidth(2)
            handle_wav.setframerate(16000)
            handle_wav.writeframes(pcm.tobytes())
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=60,
            check=False,
        )
        text = (done.stdout or b"").decode("utf-8", "replace").strip()
        return text.splitlines()[-1].strip() if text else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _google(samples: np.ndarray, rate: int) -> str:
    audio16 = _resample(samples.astype(np.float32), rate, 16000)
    pcm = np.clip(audio16 * 32768.0, -32768, 32767).astype(np.int16)
    if pcm.size < 1600:
        return ""
    try:
        from speech_recognition import AudioData, Recognizer, RequestError, UnknownValueError
    except Exception as exc:
        print("audio: speech_recognition indisponivel:", exc)
        return ""
    try:
        heard = Recognizer().recognize_google(AudioData(pcm.tobytes(), 16000, 2), language="pt-BR")
        return (heard or "").strip()
    except UnknownValueError:
        return ""
    except RequestError as exc:
        print("audio: google stt falhou:", exc)
        return ""
    except Exception as exc:
        print("audio: stt:", exc)
        return ""


def transcribe(samples: np.ndarray, rate: int) -> str:
    if samples.size < int(rate * 0.25):
        return ""
    if _rms(samples.astype(np.float32)) < 80:
        return ""
    audio = samples.astype(np.float32)
    if np.max(np.abs(audio)) > 2:
        audio = audio / 32768.0
    heard = _google(audio, rate)
    if heard:
        return heard
    pcm = np.clip(audio * 32768.0, -32768, 32767).astype(np.int16)
    return _sapi_wave(pcm, rate)


def listen(seconds: float = 8.0) -> tuple[str, list[str]]:
    devices = _ranked("in")[:3]
    used: list[str] = []
    if not devices:
        text = _sapi_listen(int(seconds))
        return text, ["microfone padrao do Windows"] if text else []
    try:
        blip()
    except Exception as exc:
        print("audio blip:", exc)
    for position, device in enumerate(devices):
        used.append(device["name"])
        try:
            samples, rate, voiced = _record(device, seconds if position == 0 else 4.0)
        except Exception as exc:
            print(f"audio: falha em {device['name']}: {exc}")
            continue
        if not voiced:
            continue
        text = _to_text(samples.astype(np.float32) / 32768.0, rate)
        if text:
            return text, used
        return "", used
    return "", used
