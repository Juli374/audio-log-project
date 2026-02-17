"""Sound feedback — currently disabled (all visual)."""

from config import Config


class Feedback:
    def __init__(self, config: Config) -> None:
        pass

    def on_record_start(self) -> None:
        pass

    def on_record_stop(self) -> None:
        pass

    def on_error(self) -> None:
        pass
