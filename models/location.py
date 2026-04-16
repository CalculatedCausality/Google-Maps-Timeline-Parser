from dataclasses import dataclass, field


@dataclass
class Location:
    timestamp: str
    latitude: float
    longitude: float
    semantic_type: str = field(default='')
