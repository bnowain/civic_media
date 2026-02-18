# Installation Guide

> **Platform**: Windows 10/11, Python 3.11, NVIDIA RTX 5090 (CUDA 12.8)
> This guide includes required patches for PyTorch nightly compatibility.

---

## Prerequisites

- Python 3.11
- Git
- FFmpeg installed and on PATH
- NVIDIA RTX 5090 with CUDA 12.8 drivers
- HuggingFace account with access to gated models (see step 4)

---

## 1. Clone and create virtual environment

```powershell
git clone https://github.com/bnowain/civic_media.git
cd civic_media
python -m venv venv
venv\Scripts\activate
```

---

## 2. Install PyTorch nightly (required for RTX 5090 / CUDA 12.8)

```powershell
pip install --pre torch torchaudio --index-url https://download.pytorch.org/whl/nightly/cu128
```

> **Why nightly?** The RTX 5090 requires CUDA 12.8 support which is only available in PyTorch nightly builds (`2.12.0.dev+cu128`). Stable PyTorch does not support this GPU yet.

---

## 3. Install Python dependencies

```powershell
pip install -r requirements.txt --break-system-packages
```

---

## 4. HuggingFace token and gated model access

Create a HuggingFace account and generate an access token at https://huggingface.co/settings/tokens

Accept the license agreements for these gated models (must be done in browser while logged in):
- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/segmentation-3.0

Set your token as an environment variable:

```powershell
# Add to your system environment variables permanently:
[System.Environment]::SetEnvironmentVariable("HF_TOKEN", "hf_your_token_here", "User")
```

Or set it per-session:
```powershell
$env:HF_TOKEN = "hf_your_token_here"
```

---

## 5. Apply library patches

PyTorch nightly (`2.12.0.dev+cu128`) breaks several APIs that pyannote.audio 3.3.2,
SpeechBrain 1.0.3, and lightning_fabric depend on. These patches must be applied after
every venv rebuild.

Save each script below and run it with `python <scriptname>.py`

### patch_speechbrain.py
Fixes `torchaudio.list_audio_backends()` removed in nightly.
```python
p = r'venv\Lib\site-packages\speechbrain\utils\torch_audio_backend.py'
c = open(p).read()
c = c.replace(
    'available_backends = torchaudio.list_audio_backends()',
    'available_backends = getattr(torchaudio, "list_audio_backends", lambda: [])()'
)
open(p, 'w').write(c)
print('Done')
```

### patch_pyannote_io.py
Fixes `torchaudio.AudioMetaData` and `torchaudio.list_audio_backends` in pyannote io.py,
and replaces `torchaudio.load` with soundfile to bypass broken torchcodec backend.
```python
p = r'venv\Lib\site-packages\pyannote\audio\core\io.py'
c = open(p).read()

# Fix AudioMetaData type annotation
c = c.replace('torchaudio.AudioMetaData', 'object')

# Fix list_audio_backends calls
c = c.replace(
    'torchaudio.list_audio_backends()',
    'getattr(torchaudio, "list_audio_backends", lambda: ["soundfile"])()'
)

# Replace torchaudio.load with soundfile for main audio loading
c = c.replace(
    'waveform, sample_rate = torchaudio.load(file["audio"], backend=self.backend)',
    'import soundfile as _sf; import torch as _t; _d, sample_rate = _sf.read(file["audio"], dtype="float32", always_2d=True); waveform = _t.from_numpy(_d.T)'
)

# Replace torchaudio.load for cropped segment loading
c = c.replace(
    '                data, _ = torchaudio.load(\n                    file["audio"],\n                    frame_offset=start_frame,\n                    num_frames=num_frames,\n                    backend=self.backend,\n                )',
    '                import soundfile as _sf2; import torch as _t2; _d2, _ = _sf2.read(file["audio"], dtype="float32", always_2d=True, start=start_frame, frames=num_frames); data = _t2.from_numpy(_d2.T)'
)

# Replace torchaudio.info with soundfile equivalent
c = c.replace(
    '    info = torchaudio.info(file["audio"], backend=backend)',
    '    import soundfile as _sf3; _i = _sf3.info(file["audio"]); info = type("I", (), {"sample_rate": _i.samplerate, "num_frames": _i.frames, "num_channels": _i.channels})()'
)

open(p, 'w').write(c)
print('Done')
```

### patch_pyannote_mixins.py
Fixes `from torchaudio import AudioMetaData` import error.
```python
p = r'venv\Lib\site-packages\pyannote\audio\tasks\segmentation\mixins.py'
c = open(p).read()
c = c.replace('from torchaudio import AudioMetaData', 'AudioMetaData = object')
open(p, 'w').write(c)
print('Done')
```

### patch_pyannote_pipeline.py
Fixes `use_auth_token` → `token` API change and strips token from `Klass(**params)` call.
```python
import re

p = r'venv\Lib\site-packages\pyannote\audio\core\pipeline.py'
c = open(p).read()

# Replace use_auth_token with token throughout
c = c.replace('use_auth_token', 'token')

# Strip token from Klass instantiation (SpeakerDiarization doesn't accept it)
c = c.replace(
    'pipeline = Klass(**params)',
    'params.pop("token", None); pipeline = Klass(**params)'
)

open(p, 'w').write(c)
print('Done')
```

### patch_pyannote_all_use_auth_token.py
Blanket fix for any remaining `use_auth_token` references across pyannote.
Run this after the above patches.

```powershell
Get-ChildItem -Recurse -Path 'venv\Lib\site-packages\pyannote' -Filter '*.py' | ForEach-Object { $c = Get-Content $_.FullName -Raw; if ($c -match 'use_auth_token') { $c = $c.Replace('use_auth_token','token'); Set-Content $_.FullName $c; Write-Host "Patched: $($_.FullName)" } }
```

### patch_lightning_torchload.py
Fixes `torch.load` defaulting to `weights_only=True` in PyTorch 2.6+, which breaks
pyannote model checkpoint loading.
```python
p = r'venv\Lib\site-packages\lightning_fabric\utilities\cloud_io.py'
c = open(p).read()

c = c.replace(
    'return torch.load(\n            path_or_url,\n            map_location=map_location,',
    'return torch.load(\n            path_or_url,\n            map_location=map_location,\n            weights_only=False,'
)
c = c.replace(
    'return torch.load(\n            f,\n            map_location=map_location,',
    'return torch.load(\n            f,\n            map_location=map_location,\n            weights_only=False,'
)

open(p, 'w').write(c)
print('Done')
```

> **Note**: If `cloud_io.py` already contains `weights_only=` in those calls, running this
> patch will create a duplicate keyword argument error. Check with:
> ```powershell
> Select-String -Path 'venv\Lib\site-packages\lightning_fabric\utilities\cloud_io.py' -Pattern 'weights_only'
> ```
> If it already exists, skip this patch.

---

## 6. Configure the database

```powershell
python -m alembic upgrade head
```

Or if not using Alembic:
```powershell
python -c "from app.database import Base, engine; from app import models; Base.metadata.create_all(engine)"
```

---

## 7. Run the application

**Terminal 1 — FastAPI server:**
```powershell
cd E:\0-Automated-Apps\civic_media
venv\Scripts\activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Terminal 2 — Celery worker:**
```powershell
cd E:\0-Automated-Apps\civic_media
venv\Scripts\activate
celery -A app.worker worker --loglevel=info --concurrency=1 --pool=solo
```

Open http://localhost:8000

---

## 8. Verify diarization works

Before processing a real meeting, test the diarization stack in isolation:

```python
# test_diarizer.py (already in repo root)
import os, sys
sys.path.insert(0, r'E:\0-Automated-Apps\civic_media')
os.environ['HF_TOKEN'] = 'hf_your_token_here'
from app.services.diarizer import diarize
results = diarize(r'path\to\any\audio.wav')
print(f'Got {len(results)} speaker turns')
```

```powershell
python test_diarizer.py
```

Expected output: `Got NNNN speaker turns` followed by sample turns.

---

## Troubleshooting

### `SpeechBrain could not find any working torchaudio backend`
Non-fatal warning. SpeechBrain falls back to soundfile after the patches. Processing continues normally.

### `triton not found; flop counting will not work`
Non-fatal warning. Triton is not available on Windows. No impact on functionality.

### `TensorFloat-32 (TF32) has been disabled`
Non-fatal warning from pyannote for reproducibility. No impact on output quality.

### `_pickle.UnpicklingError: Weights only load failed`
Apply `patch_lightning_torchload.py` from step 5.

### `TypeError: hf_hub_download() got an unexpected keyword argument 'use_auth_token'`
Apply `patch_pyannote_all_use_auth_token.py` from step 5.

### `RuntimeError: Could not load libtorchcodec`
Apply `patch_pyannote_io.py` from step 5 (replaces torchaudio.load with soundfile).

### Processing stalls and system slows to a crawl
Normal during the embedding phase (~1700 segments for a 3-hour meeting). Memory usage
will be high (50-70GB on a 128GB system). The embedding loop runs sequentially —
this is a known bottleneck for future optimization.

### After a crash, pipeline restarts from scratch
The pipeline is idempotent with three checkpoints (audio, transcript, embeddings).
If it restarts from scratch, check that the `TranscriptSegment` rows were committed
before the crash — they should have `embedding = NULL` which signals resume-from-diarization.
