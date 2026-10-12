import receivables
import receivables.agent
import receivables.core


def test_package_imports_with_version() -> None:
    assert receivables.__version__ == "0.1.0"


def test_core_and_agent_packages_import() -> None:
    assert receivables.core.__all__ == []
    assert receivables.agent.__all__ == []
