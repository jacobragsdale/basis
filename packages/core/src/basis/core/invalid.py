"""A pydantic validation error as the one line `basis` prints: for settings, each variable to set; for anything else,
each value to fix, under the title of what was validated, such as a model whose config names it
`pyproject.toml's [tool.basis.qa]`."""

from pydantic import ValidationError

from basis.core import logs, settings


def explain(error: ValidationError) -> str:
    """`error` as each value it refuses, ending with the fix: for settings, the variable that sets each."""
    variable = {"Settings": settings.variable, "FileSettings": settings.variable, "LogSettings": logs.variable}.get(error.title)
    if variable is not None:
        variables = "; ".join(f"{variable(problem['loc'])}: {problem['msg']}" for problem in error.errors(include_url=False, include_input=False))
        return f"the settings are not valid: {variables}: set each variable named, as .env.example shows"
    return f"{error.title} is not valid: {named(error)}: fix each value named"


def named(error: ValidationError) -> str:
    """Each value `error` refuses, by where it is, as `marker: String should match pattern ...; stores: ...`."""
    return "; ".join(f"{'.'.join(map(str, problem['loc'])) or 'the value'}: {problem['msg']}" for problem in error.errors(include_url=False, include_input=False))
