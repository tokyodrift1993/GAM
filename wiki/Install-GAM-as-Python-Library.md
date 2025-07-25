# Install GAM as Python Library


You can install GAM as a Python library with pip.
```
pip install gam7
```

Or as a PEP 508 Requirement Specifier, e.g. in requirements.txt file:
```
gam7
```

Or a pyproject.toml file:
```
[project]
name = "your-project"
# ...
dependencies = [
    "gam7"
]
```

Target a specific version:
```
gam7==/7.13.3
```

## Using the library

```
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
""" Sample Python script to call GAM"""

import multiprocessing
import platform

from gam import initializeLogging, CallGAMCommand

if __name__ == '__main__':
# One time initialization
  if platform.system() != 'Linux':
    multiprocessing.freeze_support()
    multiprocessing.set_start_method('spawn')
  initializeLogging()
#
  CallGAMCommand(['gam', 'version'])
  # Issue command, output goes to stdout/stderr
  rc = CallGAMCommand(['gam', 'info', 'domain'])
  # Issue command, redirect stdout/stderr
  rc = CallGAMCommand(['gam', 'redirect', 'stdout', 'domain.txt', 'redirect', 'stderr', 'stdout', 'info', 'domain'])
```
