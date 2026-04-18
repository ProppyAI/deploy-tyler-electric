"""Abstract base class for HARNESS channel adapters."""

from abc import ABC, abstractmethod


class ChannelAdapter(ABC):
    """Base class all channel adapters must inherit from.

    Each adapter normalizes messages to the HARNESS message schema
    and can send replies back through its channel.
    """

    @abstractmethod
    def receive(self) -> list:
        """Poll for new messages since last check.

        Returns list of dicts matching the message schema:
        {id, channel, from, to, text, thread_id, timestamp, metadata}
        """
        ...

    @abstractmethod
    def send(self, recipient: str, text: str) -> bool:
        """Send a response through this channel.

        Args:
            recipient: Channel-specific recipient ID (phone number, chat_id, etc.)
            text: Response text to send

        Returns:
            True if sent successfully, False otherwise.
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this adapter can run in the current environment.

        Returns False for iMessage on non-macOS, Telegram without token, etc.
        """
        ...
