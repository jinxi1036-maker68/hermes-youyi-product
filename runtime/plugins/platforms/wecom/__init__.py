def register(*args, **kwargs):
    from .adapter import register as _register

    return _register(*args, **kwargs)

__all__ = ["register"]
