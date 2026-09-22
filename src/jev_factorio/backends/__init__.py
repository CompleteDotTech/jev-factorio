def has_adapter(native: object, adapter_type: type) -> bool:
    while native is not None:
        if isinstance(native, adapter_type):
            return True
        native = getattr(native, "native", None)
    return False
