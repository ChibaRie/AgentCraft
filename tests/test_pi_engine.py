import pytest

from backend.engine.event_handler import EventHandler
from backend.engine.extension_generator import ExtensionGenerator
from backend.engine.pi_engine import PiEngine
from backend.engine.pi_engine_manager import PiEngineManager
from backend.engine.skill_loader import SkillLoader


@pytest.mark.parametrize(
    "engine",
    [PiEngine(), PiEngineManager(), ExtensionGenerator(), SkillLoader(), EventHandler()],
)
def test_pi_engine_boundaries_are_placeholder(engine) -> None:
    with pytest.raises(NotImplementedError):
        engine.start()
