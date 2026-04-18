"""Compatibility entry point for the Meta Hackathon inference baseline."""

if __package__:
    from .agent.runner import main
else:  # pragma: no cover - direct script execution
    from agent.runner import main


if __name__ == "__main__":
    main()
