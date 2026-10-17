"""L'assemblage natif conserve CodecDelay sans le compenser une seconde fois."""
import pytest

from core.matroska.ebml import ascii_element, element, uint_element
from core.matroska.reader import MatroskaReader
from core.workflows.encode.runtime.native_mux import _rewritten_audio_time_shift
from tests.test_matroska_mux_unification import _write_mkv


@pytest.mark.parametrize("delay_ns", [0, 5_333_333, 21_333_333])
@pytest.mark.parametrize("block_ms", [0, 5, 125])
def test_native_origin_uses_blocks_and_preserves_codec_delay(tmp_path, block_ms, delay_ns):
    # Les ffprobe 6/8 présentent CodecDelay différemment ; les blocs de cet
    # artefact déjà recalé restent identiques et suffisent au mux natif.
    payload = b"".join((
        uint_element(b"\xd7", 1), uint_element(b"\x73\xc5", 1),
        uint_element(b"\x83", 2), ascii_element(b"\x86", "A_AC3"),
        uint_element(b"\x56\xaa", delay_ns),
    ))
    entry = element(b"\xae", payload)
    cluster = element(b"\x1f\x43\xb6\x75", uint_element(b"\xe7", block_ms)
                      + element(b"\xa3", b"\x81\x00\x00\x80" + b"\0" * 16))
    artifact = _write_mkv(tmp_path / "rewritten.mka", [entry], clusters=cluster)
    assert _rewritten_audio_time_shift(artifact) == -block_ms
    assert MatroskaReader(artifact).tracks()[0].raw_entry == payload


def test_empty_rewritten_audio_has_no_extra_offset(tmp_path):
    artifact = _write_mkv(tmp_path / "empty.mka", [])
    assert _rewritten_audio_time_shift(artifact) == 0
