def split_page(result):
    if isinstance(result, list):
        return result, None
    if isinstance(result, dict):
        chunk = None
        for key in ("items", "entities", "states"):
            if isinstance(result.get(key), list):
                chunk = result[key]
                break
        if chunk is None:
            raise ConnectionError("unexpected ha page")
        cursor = result.get("next") or result.get("next_page") or result.get("cursor")
        if cursor in ("", None, 0, False):
            return chunk, None
        return chunk, cursor
    raise ConnectionError("unexpected ha result")
