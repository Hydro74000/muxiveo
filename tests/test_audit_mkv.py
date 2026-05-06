from __future__ import annotations

from audit_mkv import (
    AuditReport,
    DoviAudit,
    DoviBlockMappingAudit,
    FilenameAudit,
    Hdr10PlusAudit,
    HevcAudit,
    HdrStaticSummary,
    PacketAudit,
    WorkflowAudit,
    _build_workflow_audit,
    _build_findings,
    _detect_hdr_mode,
)


def _base_report() -> AuditReport:
    return AuditReport(
        input_path="/tmp/test.mkv",
        container={
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "color_transfer": "smpte2084",
                    "color_primaries": "bt2020",
                    "color_space": "bt2020nc",
                    "disposition": {"attached_pic": 0},
                }
            ]
        },
        packet_audit=PacketAudit(),
        hevc_audit=HevcAudit(),
        dovi_audit=DoviAudit(),
        hdr10plus_audit=Hdr10PlusAudit(),
        filename_audit=FilenameAudit(),
        workflow_audit=WorkflowAudit(),
        findings=[],
    )


def test_detect_hdr_mode_identifies_dv_hdr10plus_hdr10():
    report = _base_report()
    report.hevc_audit.static_hdr = HdrStaticSummary(
        unique_mastering_display=["md"],
        unique_content_light=["1000,400"],
    )
    report.hevc_audit.access_units_with_hdr10plus = 100
    report.dovi_audit.ffprobe_record = {"dv_profile": 8}
    report.hdr10plus_audit.verify_ok = True

    mode = _detect_hdr_mode(
        container=report.container,
        hevc_audit=report.hevc_audit,
        dovi_audit=report.dovi_audit,
        hdr10plus_audit=report.hdr10plus_audit,
    )

    assert mode.has_dovi is True
    assert mode.has_hdr10plus is True
    assert mode.has_hdr10 is True
    assert mode.label == "DoVi + HDR10+ + HDR10"


def test_build_workflow_audit_matches_expected_nvenc_path_for_dv_hdr10plus():
    report = _base_report()
    report.packet_audit.non_monotonic_pts = 0
    report.packet_audit.non_monotonic_dts = 0
    report.hevc_audit.access_units = 100
    report.hevc_audit.key_access_units = 4
    report.hevc_audit.key_access_units_with_param_sets = 4
    report.hevc_audit.first_access_unit_has_vps = True
    report.hevc_audit.rpu_nal_count = 100
    report.hevc_audit.access_units_with_hdr10plus = 100
    report.hevc_audit.access_units_with_mdcv = 4
    report.hevc_audit.access_units_with_cll = 4
    report.hevc_audit.static_hdr = HdrStaticSummary(
        unique_mastering_display=["md"],
        unique_content_light=["1000,400"],
    )
    report.dovi_audit.ffprobe_record = {"dv_profile": 8}
    report.dovi_audit.block_mapping = DoviBlockMappingAudit(has_dovi_block_addition=True)
    report.hdr10plus_audit.verify_ok = True

    workflow = _build_workflow_audit(report, workflow_codec="hevc_nvenc")

    assert workflow.detected_mode.label == "DoVi + HDR10+ + HDR10"
    assert workflow.metadata_inject_required is True
    assert workflow.static_hdr_mode == "bitstream_patch"
    assert workflow.overall_consistent is True
    assert workflow.observed_checks["hdr10plus_present_in_all_access_units"] is True
    assert workflow.observed_checks["dovi_block_mapping_present"] is True
    assert any("Extract Dolby Vision RPU" in step for step in workflow.expected_steps)
    assert any("Inject HDR10+ metadata" in step for step in workflow.expected_steps)
    assert any("Patch static HDR10 SEI" in step for step in workflow.expected_steps)


def test_build_findings_reports_trail_n_on_dovi_stream():
    report = _base_report()
    report.dovi_audit.ffprobe_record = {"dv_profile": 8}
    report.hevc_audit.trail_n_nals = 42
    report.workflow_audit = _build_workflow_audit(report, workflow_codec="hevc_nvenc")

    findings = _build_findings(report)

    assert any("TRAIL_N" in finding.message for finding in findings)
