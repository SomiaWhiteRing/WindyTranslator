import os
from dataclasses import dataclass
from typing import Literal, Optional

EngineType = Literal["rm200x", "vxace", "mvmz"]


@dataclass(frozen=True)
class DetectedGame:
    engine: EngineType
    reason: str


def detect_game_engine(game_path: str) -> Optional[DetectedGame]:
    """
    Best-effort engine detection by file layout.

    - RM2000/2003: RPG_RT.lmt exists in game root.
    - RPG Maker VX Ace: Data/MapInfos.rvdata2 exists.
    - RPG Maker MV/MZ: data/MapInfos.json or www/data/MapInfos.json exists.
    """
    if not game_path:
        return None

    lmt_path = os.path.join(game_path, "RPG_RT.lmt")
    if os.path.isfile(lmt_path):
        return DetectedGame(engine="rm200x", reason="found RPG_RT.lmt")

    map_infos = os.path.join(game_path, "Data", "MapInfos.rvdata2")
    if os.path.isfile(map_infos):
        return DetectedGame(engine="vxace", reason="found Data/MapInfos.rvdata2")

    for rel in (os.path.join("data", "MapInfos.json"), os.path.join("www", "data", "MapInfos.json")):
        map_infos_json = os.path.join(game_path, rel)
        if os.path.isfile(map_infos_json):
            return DetectedGame(engine="mvmz", reason=f"found {rel}")

    return None
