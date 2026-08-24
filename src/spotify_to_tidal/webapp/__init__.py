"""Mobile-friendly, read-only favorites audit application."""


def main() -> None:
    try:
        from .app import main as run
    except ImportError as exc:  # pragma: no cover - exercised by installations without extras
        raise SystemExit(
            'The web app dependencies are missing. Install with: pip install -e ".[web]"'
        ) from exc
    run()


__all__ = ["main"]
