"""Command line: argument parsing, terminal rendering, and the console.

Deliberately empty of code. Python executes a package's ``__init__`` before any
submodule, so anything imported here is paid for by every importer of
``cli.parser``, ``cli.render``, or ``cli.console`` — including the pure
renderers, which would otherwise need the gateway's dependencies to import.
The command bodies live in ``commands.py`` for that reason.
"""
