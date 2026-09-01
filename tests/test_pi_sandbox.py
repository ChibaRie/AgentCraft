from backend.engine import (
    event_handler,
    extension_generator,
    pi_engine,
    pi_engine_manager,
    skill_loader,
)


def test_sandbox_modules_have_no_real_docker_implementation() -> None:
    modules = [pi_engine, pi_engine_manager, extension_generator, skill_loader, event_handler]
    assert all(not hasattr(module, "docker") for module in modules)
    assert all(not hasattr(module, "subprocess") for module in modules)
