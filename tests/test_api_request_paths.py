"""Requêtes de l'API : jamais de dossier courant implicite, chemins inutilisables refusés, redirections refusées."""

from __future__ import annotations

import os
from pathlib import Path
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
    named, _ = MediaJobRequest.parse({"job": {"sources": [str(tmp_path / "a.mkv")], "output": str(tmp_path / "o.mkv"),
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


def test_internal_keys_of_a_request_document_are_ignored(tmp_path):
    from core.service.ops.media import MediaJobRequest

    request, _ = MediaJobRequest.parse({"job": {"sources": [str(tmp_path / "a.mkv")], "output": str(tmp_path / "o"),
                                                "_batch_output_dir": "/ailleurs", "_allow_missing_output_dir": True}})
    assert "_batch_output_dir" not in request.job and "_allow_missing_output_dir" not in request.job
    legacy, _ = MediaJobRequest.parse({"job": {"sources": ["a.mkv"], "_batch_output_dir": "/x"}},
                                      allow_relative=True, strict=False, strict_documents=False)
    assert legacy.job["_batch_output_dir"] == "/x"  # documents du CLI historique : inchangés


def test_profile_request_metadata_is_a_closed_job_subset(tmp_path):
    from core.service.ops.profile import ProfileApplyRequest

    base = {"profile": "Film", "inputs": [str(tmp_path / "a.mkv")]}
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
        inspection.inspect_sources({"sources": ["a.mkv"], "ffprobe": "/tmp/evil", "mediainfo": "/tmp/evil"},
                                   config, CommonOptions(), SimpleNamespace(emit=lambda *a, **k: None))
    assert seen == {"ffprobe": "/opt/ffprobe", "mediainfo": "/opt/mediainfo"}


def test_tmdb_cover_must_come_from_the_tmdb_image_cdn():
    from core.jobs.contract import strict_job_errors, validate_job_contract
    from core.jobs.errors import ContractError

    ok = {"version": 1, "sources": ["a.mkv"], "tmdb_cover": ["https://image.tmdb.org/t/p/original/x.jpg", "c.jpg"]}
    validate_job_contract(ok, path="job")
    with pytest.raises(ContractError, match="tmdb_cover"):
        validate_job_contract({**ok, "tmdb_cover": ["http://192.168.1.1/admin", "c.jpg"]}, path="job")
    assert strict_job_errors(ok, path="job") == []


def test_preview_capture_needs_a_folder_without_reference(tmp_path):
    from core.service import ExecutionContext, ListSink, call
    from core.service.catalog import CATALOG

    ctx = ExecutionContext.create(SimpleNamespace(), ListSink())
    with pytest.raises(ApiError) as caught:
        call(CATALOG, "encode.preview_capture",
             {"job": {"sources": [str(tmp_path / "a.mkv")], "output": str(tmp_path / "o.mkv")}}, ctx)
    assert caught.value.code is ErrorCode.VALIDATION and "output_dir" in caught.value.message


def test_two_pass_log_cleanup_never_touches_the_process_cwd(tmp_path, monkeypatch):
    from core.workflows.encode.runtime.video_preparation import TwoPassLogCleanupService

    monkeypatch.chdir(tmp_path)
    log = tmp_path / "ffmpeg2pass-0.log"
    log.write_text("x", encoding="utf-8")
    TwoPassLogCleanupService.cleanup(None)
    assert log.exists()


@pytest.mark.skipif(os.name != "nt", reason="conventions Windows")
def test_windows_drive_relative_and_rooted_paths_are_refused(tmp_path):
    from core.service.ops.media import MediaJobRequest

    for value in ("C:film.mkv", r"\film.mkv"):
        with pytest.raises(ApiError) as caught:
            MediaJobRequest.parse({"job": {"sources": [value], "output": str(tmp_path / "o.mkv")}}, base_dir=tmp_path)
        assert "inutilisable" in _details(caught.value)["job.sources[0]"]


def test_empty_or_padded_sources_are_classified_as_used(tmp_path):
    from core.service.ops.media import MediaJobRequest

    output = str(tmp_path / "o.mkv")
    with pytest.raises(ApiError) as caught:
        MediaJobRequest.parse({"job": {"sources": [""], "output": output}}, base_dir=tmp_path)
    assert _details(caught.value)["job.sources[0]"] == "chemin attendu"
    with pytest.raises(ApiError) as caught:  # « ␠/abs » est relatif tel qu'il sera utilisé
        MediaJobRequest.parse({"job": {"sources": [" " + str(tmp_path / "a.mkv")], "output": output}})
    assert "job.sources[0]" in _details(caught.value)


def test_profile_request_without_metadata_keeps_its_default(tmp_path):
    from core.service.ops.profile import ProfileApplyRequest

    request, _ = ProfileApplyRequest.parse({"profile": "Film", "inputs": [str(tmp_path / "a.mkv")]})
    assert request.metadata == {}


def test_registered_profile_wins_over_a_homonym_in_the_cwd(tmp_path, monkeypatch):
    import json

    from core.jobs.profile import load_decision_profile, snapshot_decision_profile
    from core.profiles.decision import DecisionProfileManager

    registered = {"version": 1, "kind": "decision-profile", "name": "P"}
    DecisionProfileManager(tmp_path / "profils" / "decision").save(registered)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "P.json").write_text(json.dumps({**registered, "name": "Intrus"}), encoding="utf-8")
    monkeypatch.chdir(cwd)
    config = SimpleNamespace(profiles_dir=tmp_path / "profils")
    assert load_decision_profile("P", config, base_dir=None)["name"] == "P"
    assert snapshot_decision_profile("P", config, base_dir=None)[1]["name"] == "P"
    assert load_decision_profile("P", config)["name"] == "Intrus"  # commande historique : dossier courant


def test_batch_file_documents_are_checked_and_cleaned_for_api_requests(tmp_path):
    from core.jobs.batch import api_file_document

    clean = api_file_document({"sources": [str(tmp_path / "a.mkv")], "_batch_output_dir": "."}, tmp_path, "template")
    assert clean == {"sources": [str(tmp_path / "a.mkv")]}
    if os.name != "nt":
        with pytest.raises(JobError) as caught:
            api_file_document({"sources": ["C:/film.mkv"]}, tmp_path, "jobs[0]")
        assert "jobs[0].sources[0]" in str(caught.value)


@pytest.mark.skipif(os.name == "nt", reason="conventions POSIX")
def test_rendered_output_of_another_os_is_refused(tmp_path):
    from core.jobs.remux_config import resolve_final_output

    with pytest.raises(JobError, match="inutilisable"):
        resolve_final_output(cli_output=None, job={"output_template": "C:/sorties/film.mkv"},
                             sources=[SimpleNamespace(path=tmp_path / "Film.mkv")], details=None, base_dir=tmp_path)


def test_admission_and_execution_share_one_reference_folder(tmp_path):
    from dataclasses import dataclass

    from core.service import ExecutionContext, ListSink, Model, call, field, operation
    from core.service.operations import Catalog

    @dataclass(frozen=True)
    class _Req(Model):
        source: Path = field(description="Fichier.")

    @dataclass(frozen=True)
    class _Res(Model):
        source: str
        base: str

    @operation("test.base", request=_Req, result=_Res, summary="Base effective.")
    def _op(request: _Req, ctx: ExecutionContext) -> _Res:
        return _Res(source=str(request.source), base=str(ctx.base_dir))

    catalog = Catalog((_op,))
    ctx = ExecutionContext.create(SimpleNamespace(), ListSink(), base_dir=tmp_path / "ctx")
    result = call(catalog, "test.base", {"source": "a.mkv"}, ctx)
    assert (result.source, result.base) == (str(tmp_path / "ctx" / "a.mkv"), str(tmp_path / "ctx"))
    result = call(catalog, "test.base", {"source": "a.mkv"}, ctx, base_dir=tmp_path / "req")
    assert (result.source, result.base) == (str(tmp_path / "req" / "a.mkv"), str(tmp_path / "req"))


def test_input_alias_list_keeps_each_entry():
    from core.jobs.inspection import source_path_items

    assert [item["path"] for item in source_path_items({"input": ["/m/a.mkv", "/m/b.mkv"]})] == ["/m/a.mkv", "/m/b.mkv"]
    assert [item["path"] for item in source_path_items({"input": "/m/a.mkv"})] == ["/m/a.mkv"]


@pytest.mark.parametrize("value", ["C:film.json", " C:film ", "C:/profils/film.json"])
def test_profile_references_unusable_here_are_refused_everywhere(value, tmp_path):
    from core.jobs.profile import _profile_path_candidates
    from core.json_documents import path_kind
    from core.service.ops.profile import ProfileApplyRequest, ProfileRequest

    if path_kind(value.strip()) != "unusable":
        pytest.skip("chemin utilisable sur cet OS")
    with pytest.raises(ApiError):
        ProfileRequest.parse({"profile": value})
    with pytest.raises(ApiError):
        ProfileApplyRequest.parse({"profile": value, "inputs": [str(tmp_path / "a.mkv")]})
    config = SimpleNamespace(profiles_dir=tmp_path / "profils")
    candidates = _profile_path_candidates(value, config, base_dir=tmp_path)
    assert all(str(path).startswith(str(tmp_path / "profils")) for path in candidates)  # noms enregistrés seulement


@pytest.mark.parametrize("template", ["\\\\sortie.mkv", "C:/sorties/film.mkv", "../film.mkv"])
def test_hybrid_rendered_names_stay_in_the_output_folder(template):
    from core.jobs.output_template import render_output_filename

    with pytest.raises(ValueError, match="dossier de sortie"):
        render_output_filename(template, {})


def test_profile_output_is_checked_as_it_will_be_used(tmp_path):
    from core.service.ops.profile import ProfilePreviewRequest

    with pytest.raises(ApiError) as caught:  # « ␠/abs » : relatif tel qu'il sera utilisé, sans dossier de référence
        ProfilePreviewRequest.parse({"profile": "Film", "inputs": [str(tmp_path / "a.mkv")],
                                     "output": " " + str(tmp_path / "o.mkv")})
    assert "output" in _details(caught.value)


@pytest.mark.parametrize("preset", ["C:rapide", "C:/presets/rapide"])
def test_unusable_preset_paths_are_refused_without_probing(preset, tmp_path, monkeypatch):
    from core.jobs.encode import preset_loader
    from core.json_documents import path_kind, preset_is_file
    from core.service.ops.media import MediaJobRequest

    if path_kind(preset) != "unusable":
        pytest.skip("chemin utilisable sur cet OS")
    monkeypatch.setattr(Path, "is_file", lambda self: pytest.fail(f"sonde : {self}"))
    assert preset_is_file(preset, tmp_path)
    with pytest.raises(ApiError) as caught:
        MediaJobRequest.parse({"job": {"sources": [str(tmp_path / "a.mkv")], "output": str(tmp_path / "o.mkv"),
                                       "encode": {"preset": preset}}}, base_dir=tmp_path)
    assert "job.encode.preset" in _details(caught.value)
    with pytest.raises(Exception, match="inutilisable"):
        preset_loader(tmp_path / "presets", tmp_path)(preset)


@pytest.mark.parametrize("output", [" /media/o.mkv", "   "])
def test_profile_output_padded_or_blank_is_never_resolved_from_the_cwd(output, tmp_path):
    from core.json_documents import resolve_output_reference
    from core.service.ops.profile import ProfilePreviewRequest

    request = {"profile": "Film", "inputs": [str(tmp_path / "a.mkv")], "output": output}
    with pytest.raises(ApiError):  # sans dossier de référence
        ProfilePreviewRequest.parse(request)
    if output.strip():
        resolved, _ = ProfilePreviewRequest.parse(request, base_dir=tmp_path)
        assert resolved.output == str(tmp_path / output)  # relatif tel qu'utilisé : sous le dossier de la requête
        assert resolve_output_reference(output, tmp_path) == str(tmp_path / output)
    else:
        with pytest.raises(ApiError):
            ProfilePreviewRequest.parse(request, base_dir=tmp_path)
