import soundfile as sf

p = r'E:\0-Automated-Apps\civic_media\venv\Lib\site-packages\pyannote\audio\core\io.py'
c = open(p).read()

c = c.replace(
    '    info = torchaudio.info(file["audio"], backend=backend)',
    '    import soundfile as _sf3; _i = _sf3.info(file["audio"]); info = type("I", (), {"sample_rate": _i.samplerate, "num_frames": _i.frames, "num_channels": _i.channels})()'
)

open(p, 'w').write(c)
print('Done')