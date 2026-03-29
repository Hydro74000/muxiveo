import sys
sys.path.insert(0, '.')
from pathlib import Path
from core.inspector import FileInspector
from core.workflows.remux import RemuxConfig, RemuxWorkflow, SourceInput, tracks_from_file_info

path = Path('/home/hydromel/Vidéos/testfile.mkv')
inspector = FileInspector()
info = inspector.inspect(path)

print('=== Pistes inspectées ===')
for v in info.video_tracks:
    print(f'  video #{v.index}: lang={v.language!r} title={v.title!r}')
for a in info.audio_tracks:
    print(f'  audio #{a.index}: lang={a.language!r} title={a.title!r}')
for s in info.subtitle_tracks:
    print(f'  sub   #{s.index}: lang={s.language!r} title={s.title!r}')

print()
tracks = tracks_from_file_info(info, file_id='test-id')
print('=== TrackEntry créés ===')
for t in tracks:
    print(f'  {t.track_type} mkv_tid={t.mkv_tid} lang={t.language!r} title={t.title!r} orig_lang={t.orig_language!r} orig_title={t.orig_title!r}')

print()
track_order = [(0, t.mkv_tid) for t in tracks if t.enabled]
config = RemuxConfig(
    sources=[SourceInput(path=path, file_index=0, tracks=tracks)],
    output=Path('/home/hydromel/Vidéos/testfile_remux.mkv'),
    track_order=track_order,
)
wf = RemuxWorkflow()
print('=== Commande buildée ===')
print(wf.preview_command(config))

