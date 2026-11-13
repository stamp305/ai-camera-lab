"""Entry point: `python client.py` starts the supervisor.

The supervisor itself lives in client_logic.py. This file only forwards to it,
for two reasons:

- It used to hold a copy of that logic — ~1700 lines already superseded by
  client_logic.py and no longer reached by anything.
- A build compiles the modules to .so and deletes their .py, so importing
  client_logic by name works both from a git checkout (client_logic.py) and
  from a built install (client_logic.so) with no change here.

It previously imported `client_core`, which is produced by the Cython build and
never committed — so on a plain `git pull` this file raised ModuleNotFoundError
before doing anything.

bootstrap.py is the other way in: it installs the encrypted-module loader first,
which a packaged install needs. This file is the plain path for a source checkout.
"""
import sys

from client_logic import main

if __name__ == '__main__':
    sys.exit(main())
