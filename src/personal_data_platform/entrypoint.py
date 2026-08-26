"""Shared runtime entrypoint."""

import argparse

ROLES = frozenset({"webhook", "fetch", "loader", "dbt", "reconciliation"})


def resolve_role(role: str) -> str:
    """Return a supported runtime role."""
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    return role


def main(argv: list[str] | None = None) -> int:
    """Validate the selected runtime role."""
    parser = argparse.ArgumentParser(description="Run Personal Data Platform")
    parser.add_argument("role", choices=sorted(ROLES))
    args = parser.parse_args(argv)
    resolve_role(args.role)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
