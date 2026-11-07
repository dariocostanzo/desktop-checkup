# Contributing

Thank you for helping improve Desktop Checkup. Small, clear changes are easiest to review.

## Run the tests

From the project directory:

```sh
python3 -m unittest discover -s tests -v
```

Run the linter with:

```sh
ruff check .
```

Ruff is used for development and CI; it is not a runtime dependency of the app.

## Add a check

1. Add a `check_*` function in `src/health_check/app.py`. Use the existing `result()` helper for the title, status, short summary, optional explanation, details, and any fix.
2. Read local information only. Do not add a network client, server request, or telemetry.
3. If a command needs a name from system output, validate that name with a strict pattern before putting it in the command argument list. Do not use `shell=True`.
4. Add the function to `CHECKS` and map it in `result_id()` if the GUI needs to refresh it.
5. Add tests for normal output, missing tools, and relevant hardware or permission edge cases. Keep tests independent of real hardware by mocking system commands.
6. Wrap any user-visible text in `gettext` (`_()`), then run the test suite and Ruff.

Avoid adding an automated fix unless it is narrowly scoped, explained in the UI, and shown in the README's administrator-fix table when it needs root access.
