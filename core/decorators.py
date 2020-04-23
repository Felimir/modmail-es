import warnings

from core.utils import trigger_typing as _trigger_typing


def trigger_typing(func):
    warnings.warn(
        "trigger_typing fue movido a core.utils.trigger_typing, éste será removido.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _trigger_typing(func)
