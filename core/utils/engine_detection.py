import os
from dataclasses import dataclass
from typing import Literal, Optional

EngineType = Literal["rm200x", "vxace"]


@dataclass(frozen=True)
class DetectedGame:
    engine: EngineType
    reason: str


def detect_game_engine(game_path: str) -> Optional[DetectedGame]:
    """
    Best-effort engine detection by file layout.

    - RM2000/2003: RPG_RT.lmt exists in game root.
    - RPG Maker VX Ace: Data/MapInfos.rvdata2 exists.
    """
    if not game_path:
        return None

    lmt_path = os.path.join(game_path, "RPG_RT.lmt")
    if os.path.isfile(lmt_path):
        return DetectedGame(engine="rm200x", reason="found RPG_RT.lmt")

    map_infos = os.path.join(game_path, "Data", "MapInfos.rvdata2")
    if os.path.isfile(map_infos):
        return DetectedGame(engine="vxace", reason="found Data/MapInfos.rvdata2")

    return None

