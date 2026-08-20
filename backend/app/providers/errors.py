from app.domain.provider import ProviderFailure


class ProviderRequestError(RuntimeError):
    """Typed provider failure exposed across adapter boundaries."""

    def __init__(self, failure: ProviderFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure
