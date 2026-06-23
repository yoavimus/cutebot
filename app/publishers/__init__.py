"""Social-network publishers. Each network is an adapter behind ``Publisher``."""

from app.publishers.base import Publisher, PublishResult, get_publishers

__all__ = ["Publisher", "PublishResult", "get_publishers"]
