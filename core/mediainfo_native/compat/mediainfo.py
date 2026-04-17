"""
Compatibility wrappers inspired by MediaInfo / MediaInfoList APIs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..engine import CLI_VERSION_TEXT, VERSION_TEXT, MediaInfoEngine

_STREAM_KIND_BY_ID: dict[int, str] = {
    0: "General",
    1: "Video",
    2: "Audio",
    3: "Text",
    4: "Other",
    5: "Image",
    6: "Menu",
}


def _normalize_stream_kind(stream_kind: int | str) -> str:
    if isinstance(stream_kind, int):
        return _STREAM_KIND_BY_ID.get(stream_kind, "General")
    return str(stream_kind)


class MediaInfo:
    """
    Compatibility surface for MediaInfo with advanced Option/Option_Static.
    """

    def __init__(self, engine: MediaInfoEngine | None = None) -> None:
        self._engine = engine or MediaInfoEngine()
        self._source: str | None = None

    def Open(self, file_name: str) -> int:
        self._source = file_name
        self._engine.report(file_name)
        return 1

    def Close(self) -> None:
        self._source = None

    def Inform(self, reserved: int = 0) -> str:
        if not self._source:
            return ""
        inform = self._engine.option("inform_get")
        output = self._engine.option("output_get")
        if inform:
            return self._engine.query_inform(self._source, inform)
        if output:
            return self._engine.render(self._source, output_mode=output)
        return self._engine.render(self._source, output_mode="text")

    def Get(
        self,
        stream_kind: int | str,
        stream_number: int,
        parameter: int | str,
        info_kind: Any = None,
        search_kind: Any = None,
    ) -> str:
        if not self._source:
            return ""
        kind = _normalize_stream_kind(stream_kind)
        report = self._engine.report(self._source)
        tracks = report.tracks_by_kind(kind)
        if stream_number < 0 or stream_number >= len(tracks):
            return ""
        track = tracks[stream_number]
        if isinstance(parameter, int):
            keys = list(track.fields.keys())
            if parameter < 0 or parameter >= len(keys):
                return ""
            return track.fields[keys[parameter]]
        key = str(parameter)
        if key in track.fields:
            return track.fields[key]
        key_with_spaces = key.replace("_", " ")
        return track.fields.get(key_with_spaces, "")

    def Count_Get(self, stream_kind: int | str, stream_number: int = -1) -> int:
        if not self._source:
            return 0
        kind = _normalize_stream_kind(stream_kind)
        tracks = self._engine.report(self._source).tracks_by_kind(kind)
        if stream_number == -1:
            return len(tracks)
        if stream_number < 0 or stream_number >= len(tracks):
            return 0
        return len(tracks[stream_number].fields)

    def State_Get(self) -> int:
        return 10000 if self._source else 0

    def Option(self, option: str, value: str = "") -> str:
        return self._engine.option(option, value)

    @staticmethod
    def Option_Static(option: str, value: str = "") -> str:
        option_norm = option.strip().lower()
        if option_norm == "info_version":
            return VERSION_TEXT
        if option_norm == "help" and not value:
            return CLI_VERSION_TEXT
        return MediaInfoEngine.option_static(option, value)


class MediaInfoList:
    """
    Compatibility surface for MediaInfoList.
    """

    def __init__(self, count_init: int = 64, engine: MediaInfoEngine | None = None) -> None:
        self._engine = engine or MediaInfoEngine()
        self._files: list[str] = []
        self._mi_objects: list[MediaInfo] = []
        self._global_options: dict[str, str] = {}

    def Open(self, file_or_directory: str, options: int = 0) -> int:
        path = Path(file_or_directory)
        added = 0
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if not child.is_file():
                    continue
                try:
                    mi = MediaInfo(self._engine)
                    if mi.Open(str(child)):
                        self._files.append(str(child))
                        self._mi_objects.append(mi)
                        added += 1
                except Exception:
                    continue
            return added

        mi = MediaInfo(self._engine)
        mi.Open(file_or_directory)
        self._files.append(file_or_directory)
        self._mi_objects.append(mi)
        return 1

    def Close(self, file_pos: int = -1) -> None:
        if file_pos == -1:
            self._files.clear()
            self._mi_objects.clear()
            return
        if 0 <= file_pos < len(self._files):
            self._files.pop(file_pos)
            self._mi_objects.pop(file_pos)

    def Inform(self, file_pos: int = -1, reserved: int = 0) -> str:
        if file_pos == -1:
            return "\n".join(mi.Inform() for mi in self._mi_objects if mi.Inform())
        if 0 <= file_pos < len(self._mi_objects):
            return self._mi_objects[file_pos].Inform()
        return ""

    def Get(
        self,
        file_pos: int,
        stream_kind: int | str,
        stream_number: int,
        parameter: int | str,
        kind_of_info: Any = None,
        kind_of_search: Any = None,
    ) -> str:
        if 0 <= file_pos < len(self._mi_objects):
            return self._mi_objects[file_pos].Get(stream_kind, stream_number, parameter, kind_of_info, kind_of_search)
        return ""

    def Count_Get(self, *args: Any) -> int:
        # Count_Get()
        if len(args) == 0:
            return len(self._mi_objects)
        # Count_Get(file_pos, stream_kind, stream_number=-1)
        if len(args) >= 2:
            file_pos = int(args[0])
            stream_kind = args[1]
            stream_number = int(args[2]) if len(args) > 2 else -1
            if 0 <= file_pos < len(self._mi_objects):
                return self._mi_objects[file_pos].Count_Get(stream_kind, stream_number)
        return 0

    def Option(self, option: str, value: str = "") -> str:
        if value:
            self._global_options[option.lower()] = value
            for mi in self._mi_objects:
                mi.Option(option, value)
            return self._engine.option(option, value)
        if option.lower() in self._global_options:
            return self._global_options[option.lower()]
        return self._engine.option(option)

    @staticmethod
    def Option_Static(option: str, value: str = "") -> str:
        return MediaInfo.Option_Static(option, value)

    def State_Get(self) -> int:
        return 10000 if self._mi_objects else 0
