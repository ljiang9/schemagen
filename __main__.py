"""Enables `python -m schemagen`."""
import sys

if __package__:
    from .schemagen import main
else:  # 直接运行 __main__.py 时的回退
    from schemagen import main

if __name__ == "__main__":
    sys.exit(main())
