from __future__ import annotations

from core.workflows.encode.models import EncodeConfig

from .plan_models import EncodeCommandSelection, EncodePlan


def build_encode_command_selection(
    config: EncodeConfig,
    *,
    plan: EncodePlan,
    is_multi_video,
    uses_two_pass,
    build_multi_video_preview,
    build_two_pass,
    build_single_pass,
    chapter_materialize_dir=None,
) -> EncodeCommandSelection:
    if is_multi_video(config):
        commands = tuple(
            tuple(cmd)
            for cmd in build_multi_video_preview(config, plan=plan)
            if cmd
        )
        preview_index = max(0, len(commands) - 1) if commands else 0
        return EncodeCommandSelection(
            commands=commands,
            preview_index=preview_index,
            is_multi_video=True,
            is_two_pass=False,
        )

    if uses_two_pass(config):
        commands = tuple(
            tuple(cmd)
            for cmd in build_two_pass(
                config,
                chapter_materialize_dir=chapter_materialize_dir,
                plan=plan,
            )
            if cmd
        )
        return EncodeCommandSelection(
            commands=commands,
            preview_index=1 if len(commands) > 1 else 0,
            is_multi_video=False,
            is_two_pass=True,
        )

    cmd = build_single_pass(
        config,
        chapter_materialize_dir=chapter_materialize_dir,
        plan=plan,
    )
    return EncodeCommandSelection(
        commands=((tuple(cmd),) if cmd else tuple()),
        preview_index=0,
        is_multi_video=False,
        is_two_pass=False,
    )
