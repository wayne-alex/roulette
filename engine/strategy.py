from schemas import StrategyEnum, BotConfig


class BaseStrategy:
    def __init__(self, config: BotConfig):
        self.config = config
        self.current_chip = config.base_chip
        self.consecutive_losses = 0

    def next_bet(self) -> float:
        """Return the chip size for the next spin."""
        return self.current_chip

    def on_result(self, won: bool):
        """Update state based on previous spin."""
        if won:
            self.on_win()
        else:
            self.on_loss()

    def on_win(self):
        pass

    def on_loss(self):
        pass

    def state(self) -> dict:
        return {
            "current_chip": self.current_chip,
            "consecutive_losses": self.consecutive_losses,
        }


class FlatStrategy(BaseStrategy):
    """Always bet the same base chip."""
    pass


class MartingaleStrategy(BaseStrategy):
    """Double bet on loss, reset on win."""

    def on_win(self):
        self.current_chip = self.config.base_chip
        self.consecutive_losses = 0

    def on_loss(self):
        self.current_chip *= 2
        self.consecutive_losses += 1


def build_strategy(config: BotConfig) -> BaseStrategy:
    if config.strategy == StrategyEnum.martingale:
        return MartingaleStrategy(config)
    return FlatStrategy(config)