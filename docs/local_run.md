# Local Run Guide

This project can run directly from source. If editable install fails because
the local Python environment cannot download build dependencies, use the
`PYTHONPATH=src` commands below.

## Run Tests

```bash
make test
```

Equivalent command:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Preview Website

```bash
make site
```

Then open:

```text
http://127.0.0.1:8000
```

## Run APlan Modules From Source

```bash
PYTHONPATH=src python3 -m aplan.strategy_cli list
PYTHONPATH=src python3 -m aplan.audit verify
PYTHONPATH=src python3 -m aplan.cli --help
```

The console commands such as `aplan`, `aplan-daily`, and `aplan-sync` are
created only after package installation succeeds. Source-mode execution is the
most reliable local fallback.

## Optional Editable Install

When network access and build dependencies are available:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

After that, console commands such as `aplan --help` should be available inside
the virtual environment.
