"""
Entry point for running nano-alice as a module: python -m nano_alice
"""

from nano_alice.cli.commands import app

if __name__ == "__main__":
    app()
