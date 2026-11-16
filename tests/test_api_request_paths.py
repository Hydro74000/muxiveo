"""Requêtes de l'API : jamais de dossier courant implicite, chemins inutilisables refusés, redirections refusées."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from core.jobs.errors import JobError
from core.service.errors import ApiError, ErrorCode


def _details(error: ApiError) -> dict[str, str]:
    return {detail.path: detail.message for detail in error.details}


def test_job_paths_need_a_reference_folder_and_never_use_the_process_cwd(tmp_path, monkeypatch):
    from core.service.ops.media import MediaJobRequest

    job = {"sources": ["a.mkv", {"path": "b.mkv", "sync": {"calibration": "c.json"}}], "output": "out.mkv",
           "work_dir": "travail", "extra_attachments": ["cover.jpg"], "chapters": {"import": "chap.xml"},
           "encode": {"preset": "presets/x.json"}}
    with pytest.raises(ApiError) as caught:
        MediaJobRequest.parse({"job": job})  # service sans dossier de référence
    assert caught.value.code is ErrorCode.INVALID_REQUEST
    assert set(_details(caught.value)) == {
        "job.sources[0]", "job.sources[1].path", "job.sources[1].sync.calibration", "job.output", "job.work_dir",
        "job.extra_attachments[0]", "job.chapters.import", "job.encode.preset"}
    for cwd in (tmp_path / "un", tmp_path / "deux"):  # le résultat ne dépend jamais du dossier courant
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        request, _ = MediaJobRequest.parse({"job": job}, base_dir=tmp_path / "req")
        assert request.job["sources"][0] == str(tmp_path / "req" / "a.mkv")
        assert request.job["work_dir"] == str(tmp_path / "req" / "travail")
        assert request.job["encode"]["preset"] == str(tmp_path / "req" / "presets/x.json")
    named, _ = MediaJobRequest.parse({"job": {"sources": ["/m/a.mkv"], "output": "/m/o.mkv",
                                              "encode": {"preset": "Film/4K"}}})
    assert named.job["encode"]["preset"] == "Film/4K"  # nom de preset enregistré : jamais un chemin


@pytest.mark.skipif(os.name == "nt", reason="conventions POSIX")
def test_paths_of_another_os_are_refused_not_attached(tmp_path):
    from core.service.ops.extract import ExtractRequest
    from core.service.ops.media import MediaJobRequest

    with pytest.raises(ApiError) as caught:
        MediaJobRequest.parse({"job": {"sources": ["C:/films/a.mkv"], "output": "/o.mkv"}}, base_dir=tmp_path)
    assert "inutilisable" in _details(caught.value)["job.sources[0]"]
    with pytest.raises(ApiError) as caught:
        ExtractRequest.parse({"source": r"C:\films\a.mkv", "all": True}, base_dir=tmp_path)
    assert "inutilisable" in _details(caught.value)["source"]


def test_internal_keys_of_a_request_document_are_ignored():
    from core.service.ops.media import MediaJobRequest

    request, _ = MediaJobRequest.parse({"job": {"sources": ["/m/a.mkv"], "output": "/m/o.mkv",
                                                "_batch_output_dir": "/ailleurs", "_allow_missing_output_dir": True}})
    assert "_batch_output_dir" not in request.job and "_allow_missing_output_dir" not in request.job
    legacy, _ = MediaJobRequest.parse({"job": {"sources": ["a.mkv"], "_batch_output_dir": "/x"}},
                                      allow_relative=True, strict=False, strict_documents=False)
    assert legacy.job["_batch_output_dir"] == "/x"  # documents du CLI historique : inchangés


def test_profile_request_metadata_is_a_closed_job_subset(tmp_path):
    from core.service.ops.profile import ProfileApplyRequest

    base = {"profile": "Film", "inputs": ["/m/a.mkv"]}
    with pytest.raises(ApiError) as caught:
        ProfileApplyRequest.parse({**base, "metadata": {"sortie": "x"}})
    assert "metadata.sortie" in _details(caught.value)
    with pytest.raises(ApiError) as caught:
        ProfileApplyRequest.parse({**base, "output": "o.mkv", "metadata": {"output": "rel/"}})
    assert {"output", "metadata.output"} <= set(_details(caught.value))
    request, _ = ProfileApplyRequest.parse({**base, "metadata": {"output_template": "{title}.mkv",
                                                                 "_batch_output_dir": "/x", "output": "sorties/"}},
                                           base_dir=tmp_path)
    assert request.metadata == {"output_template": "{title}.mkv", "output": str(tmp_path / "sorties") + os.sep}
    with pytest.raises(ApiError) as caught:
        ProfileApplyRequest.parse({**base, "profile": "profils/film.json"})  # fichier relatif sans dossier
    assert "profile" in _details(caught.value)


def test_relative_rendered_output_uses_the_reference_folder_or_is_refused(tmp_path):
    from core.jobs.remux_config import resolve_final_output

    source = SimpleNamespace(path=tmp_path / "Film.mkv")
    job = {"output_template": "sortie.mkv"}
    assert resolve_final_output(cli_output=None, job=job, sources=[source], details=None,
                                base_dir=tmp_path / "req") == tmp_path / "req" / "sortie.mkv"
    with pytest.raises(JobError) as caught:
        resolve_final_output(cli_output=None, job=job, sources=[source], details=None, base_dir=None)
    assert caught.value.code is ErrorCode.VALIDATION


def test_registered_preset_and_profile_names_never_read_the_process_cwd(tmp_path, monkeypatch):
    from core.jobs.encode import preset_loader
    from core.jobs.profile import _profile_path_candidates

    monkeypatch.chdir(tmp_path)
    (tmp_path / "rapide").write_text("{}", encoding="utf-8")  # homonyme d'un preset enregistré dans le cwd
    with pytest.raises(Exception, match="rapide"):
        preset_loader(tmp_path / "presets", None)("rapide")  # cherché parmi les presets enregistrés seulement
    config = SimpleNamespace(profiles_dir=tmp_path / "profils")
    assert all(path.is_absolute() for path in _profile_path_candidates("film.json", config, base_dir=None))
    assert tmp_path / "req" / "film.json" in _profile_path_candidates("film.json", config, base_dir=tmp_path / "req")


def test_job_tool_keys_are_never_read(monkeypatch):
    from core.jobs import inspection
    from core.jobs.options import CommonOptions

    seen: dict[str, str] = {}

    class _Inspector:
        def __init__(self, *, ffprobe_bin, mediainfo_bin, **_kwargs):
            seen.update(ffprobe=ffprobe_bin, mediainfo=mediainfo_bin)

        def inspect(self, _path):
            raise RuntimeError("arrêt du test")

    monkeypatch.setattr(inspection, "FileInspector", _Inspector)
    config = SimpleNamespace(tool_ffprobe="/opt/ffprobe", tool_mediainfo="/opt/mediainfo")
    with pytest.raises(Exception):
        inspection.inspect_sources({"sources": ["/m/a.mkv"], "ffprobe": "/tmp/evil", "mediainfo": "/tmp/evil"},
                                   config, CommonOptions(), SimpleNamespace(emit=lambda *a, **k: None))
    assert seen == {"ffprobe": "/opt/ffprobe", "mediainfo": "/opt/mediainfo"}


def test_tmdb_cover_must_come_from_the_tmdb_image_cdn():
    from core.jobs.contract import strict_job_errors, validate_job_contract
    from core.jobs.errors import ContractError

    ok = {"version": 1, "sources": ["/a.mkv"], "tmdb_cover": ["https://image.tmdb.org/t/p/original/x.jpg", "c.jpg"]}
    validate_job_contract(ok, path="job")
    with pytest.raises(ContractError, match="tmdb_cover"):
        validate_job_contract({**ok, "tmdb_cover": ["http://192.168.1.1/admin", "c.jpg"]}, path="job")
    assert strict_job_errors(ok, path="job") == []


def test_preview_capture_needs_a_folder_without_reference(tmp_path):
    from core.service import ExecutionContext, ListSink, call
    from core.service.catalog import CATALOG

    ctx = ExecutionContext.create(SimpleNamespace(), ListSink())
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "encode.preview_capture", {"job": {"sources": ["/m/a.mkv"], "output": "/m/o.mkv"}}, ctx)
    assert caught.value.code is ErrorCode.VALIDATION and "output_dir" in caught.value.message


def test_two_pass_log_cleanup_never_touches_the_process_cwd(tmp_path, monkeypatch):
    from core.workflows.encode.runtime.video_preparation import TwoPassLogCleanupService

    monkeypatch.chdir(tmp_path)
    log = tmp_path / "ffmpeg2pass-0.log"
    log.write_text("x", encoding="utf-8")
    TwoPassLogCleanupService.cleanup(None)
    assert log.exists()
