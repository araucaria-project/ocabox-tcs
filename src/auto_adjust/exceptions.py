"""Exception types for auto_adjust."""


class AdapterError(Exception):
    """Base class for adapter errors."""


class NotCalibratedError(AdapterError):
    """Predict was called before the adapter has accumulated enough data
    to give a trustworthy answer.

    Adapters should raise this (rather than return a meaningless prediction)
    when `is_calibrated()` would return False and the caller has not
    explicitly opted into low-confidence predictions.
    """


class OptionalDependencyMissing(ImportError):
    """An optional dependency required by a specific adapter is not
    installed.

    Raised at adapter *import* time (not on first call) so configuration
    errors surface early. Message includes the required `pip install`
    incantation.
    """

    def __init__(self, library: str, adapter: str, install_extra: str | None = None):
        msg = (
            f"`{adapter}` requires the optional dependency `{library}`, "
            f"which is not installed."
        )
        if install_extra:
            msg += f" Install it with: `pip install auto_adjust[{install_extra}]`"
        else:
            msg += f" Install it with: `pip install {library}`"
        super().__init__(msg)
        self.library = library
        self.adapter = adapter


class EmpiricalPointRejected(AdapterError):
    """A recorded observation was rejected by an outlier or sanity guard."""
