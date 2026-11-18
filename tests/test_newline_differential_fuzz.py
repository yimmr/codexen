import io
import random
from pathlib import Path

from codexen.core.stream_matcher import replace_streaming_auto
from tests.test_newline_reference_model import reference

def test_randomized_logical_matches_are_chunk_invariant(tmp_path: Path):
    rng = random.Random(20261118)
    for case in range(40):
        lines = [f"line-{case}-{i}" for i in range(12)]
        source_text = "".join(line + rng.choice(["\n", "\r\n"]) for line in lines)
        search = lines[rng.randrange(len(lines))] + "\n"
        replacement = "REPLACED\n"
        expected, expected_count = reference(source_text, search, replacement)
        source = tmp_path / f"case-{case}.txt"
        source.write_bytes(source_text.encode())
        outputs = []
        counts = []
        for chunk in (1, 2, 3, 7, 16, 64, 4096):
            out = io.StringIO()
            result = replace_streaming_auto(source, out, search, replacement, False, "utf-8", chunk)
            outputs.append(out.getvalue())
            counts.append(result["occurrences"])
        assert outputs == [expected] * len(outputs)
        assert counts == [expected_count] * len(counts)


def test_random_token_streams_match_independent_oracle(tmp_path: Path):
    rng = random.Random(91827)
    source = tmp_path / "tokens.txt"
    alphabet = ["a", "b", "中", "\r", "\n", "\r\n"]
    for _ in range(300):
        text = "".join(rng.choices(alphabet, k=rng.randrange(1, 45)))
        needle = "".join(rng.choices(alphabet, k=rng.randrange(1, 6)))
        replacement = "".join(rng.choices(alphabet, k=rng.randrange(5)))
        source.write_bytes(text.encode())
        for replace_all in (False, True):
            expected, count = reference(text, needle, replacement, replace_all)
            for chunk in (1, 2, 3, 7, 16, 64, 4096):
                output = io.StringIO()
                result = replace_streaming_auto(source, output, needle, replacement, replace_all, "utf-8", chunk)
                assert result["occurrences"] == count, (text, needle, chunk)
                assert output.getvalue() == expected, (text, needle, replacement, chunk)
