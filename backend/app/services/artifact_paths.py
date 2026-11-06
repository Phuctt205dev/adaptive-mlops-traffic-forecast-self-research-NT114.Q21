"""Pure ownership check shared by the uploader and resource adapters."""


def owns_key(key, prefixes):
    return isinstance(key, str) and any(key.startswith(prefix) for prefix in prefixes)
