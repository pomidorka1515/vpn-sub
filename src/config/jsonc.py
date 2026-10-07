import json

def _strip_jsonc_comments(content: str) -> str:
    """Remove JSONC comments while preserving strings and line structure."""
    result: list[str] = []
    in_string = False
    escaped = False
    in_line_comment = False
    in_block_comment = False
    index = 0

    while index < len(content):
        char = content[index]

        if in_line_comment:
            if char in "\r\n":
                in_line_comment = False
                result.append(char)
            else:
                result.append(" ")
            index += 1
            continue

        if in_block_comment:
            if char == "*" and index + 1 < len(content) and content[index + 1] == "/":
                result.extend((" ", " "))
                in_block_comment = False
                index += 2
            else:
                result.append(char if char in "\r\n" else " ")
                index += 1
            continue

        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            index += 1
        elif char == "/" and index + 1 < len(content) and content[index + 1] == "/":
            result.extend((" ", " "))
            in_line_comment = True
            index += 2
        elif char == "/" and index + 1 < len(content) and content[index + 1] == "*":
            result.extend((" ", " "))
            in_block_comment = True
            index += 2
        else:
            result.append(char)
            index += 1

    if in_block_comment:
        raise json.JSONDecodeError("Unterminated comment", content, len(content))

    return "".join(result)

def _strip_jsonc_trailing_commas(content: str) -> str:
    """Remove JSONC's optional trailing commas outside string values."""
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0

    while index < len(content):
        char = content[index]
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue

        if char == ",":
            next_index = index + 1
            while next_index < len(content) and content[next_index].isspace():
                next_index += 1
            if next_index < len(content) and content[next_index] in "]}":
                index += 1
                continue

        result.append(char)
        index += 1

    return "".join(result)
