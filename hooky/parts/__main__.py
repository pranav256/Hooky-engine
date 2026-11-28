"""Run the Hooky story-parts workflow with ``python -m Hooky.parts``."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main(prog="Hooky-parts"))

