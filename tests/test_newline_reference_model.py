"""Independent, deliberately simple token oracle for bounded fixtures."""


def tokens(text: str) -> list[tuple[str, str]]:
    result = []
    i = 0
    while i < len(text):
        if text[i:i + 2] == "\r\n":
            result.append(("\n", "\r\n"))
            i += 2
        else:
            result.append((text[i], text[i]))
            i += 1
    return result


def reference(text: str, needle: str, replacement: str, replace_all: bool = False) -> tuple[str, int]:
    source = tokens(text)
    pattern = [logical for logical, _ in tokens(needle)]
    assert pattern
    output: list[str] = []
    i = count = 0
    last = None
    while i < len(source):
        region = source[i:i + len(pattern)]
        if [logical for logical, _ in region] == pattern:
            count += 1
            style = next((raw for logical, raw in region if logical == "\n"), last or "\n")
            if replace_all or count == 1:
                output.extend(style if logical == "\n" else raw for logical, raw in tokens(replacement))
            else:
                output.extend(raw for _, raw in region)
            for logical, raw in region:
                if logical == "\n":
                    last = raw
            i += len(pattern)
        else:
            logical, raw = source[i]
            output.append(raw)
            if logical == "\n":
                last = raw
            i += 1
    return "".join(output), count


def test_reference_model_handles_crlf_as_atomic_token():
    assert reference("a\r\nb\r\nc", "a\nb", "x\ny") == ("x\r\ny\r\nc", 1)
