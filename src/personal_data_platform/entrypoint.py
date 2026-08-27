"""Module entrypoint shared by the container and local CLI."""

from personal_data_platform.cli import main

__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
