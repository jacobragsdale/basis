"""Which environment a command acts in, where its settings come from, and when it may write."""

import os
from pathlib import Path

import pytest

from basis.core import environments
from basis.core.environments import EnvError, chosen, folder, load
from basis.core.settings import Settings


@pytest.fixture(autouse=True)
def no_basis_variables(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A process that sets no BASIS_ variable, whose user's configuration folder is an empty one of the test's."""
    for name in [name for name in os.environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path / "config" / "basis"


def user_file(env: str, text: str) -> Path:
    path = Path(os.environ["XDG_CONFIG_HOME"]) / "basis" / f"{env}.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(("given", "environ", "expected"), [("qa", {}, "qa"), (None, {"BASIS_ENV": "prod"}, "prod"), ("dev", {"BASIS_ENV": "dev"}, "dev"), ("qa", {"BASIS_ENV": ""}, "qa")])
def test_the_environment_is_the_one_env_names_or_else_basis_env(given: str | None, environ: dict[str, str], expected: str) -> None:
    assert chosen(given, environ) == expected


@pytest.mark.parametrize(
    ("given", "environ", "says"),
    [
        (None, {}, "names no environment, which basis never assumes: give --env dev|qa|prod, or set BASIS_ENV"),
        ("qa", {"BASIS_ENV": "prod"}, "--env qa and BASIS_ENV=prod name different environments"),
        (None, {"BASIS_ENV": "production"}, "'production' is not an environment: name one of dev, qa, prod"),
    ],
)
def test_no_environment_or_two_or_an_unknown_one_is_refused_with_the_fix(given: str | None, environ: dict[str, str], says: str) -> None:
    with pytest.raises(EnvError, match=r"^.+: .+") as raised:
        chosen(given, environ)
    assert says in str(raised.value)


def test_the_user_s_folder_is_appdata_s_on_windows_and_xdg_s_or_the_home_s_elsewhere(tmp_path: Path) -> None:
    assert folder({"APPDATA": r"C:\Users\someone\AppData\Roaming"}, windows=True, home=tmp_path) == Path(r"C:\Users\someone\AppData\Roaming") / "basis"
    assert folder({"XDG_CONFIG_HOME": "/xdg"}, windows=False, home=tmp_path) == Path("/xdg/basis")
    assert folder({"XDG_CONFIG_HOME": ""}, windows=False, home=tmp_path) == tmp_path / ".config" / "basis"
    with pytest.raises(EnvError, match="APPDATA is not set"):
        folder({}, windows=True, home=tmp_path)


def test_a_desktop_reads_the_user_s_file_for_the_environment_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    path = user_file("qa", "BASIS_SNAPSHOT_ROOT=//files/runs-qa\nBASIS_PROD_WRITES=1\nOTHER_TOOL=1\n")
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", "//files/runs-prod")  # set for another environment, which never mixes in
    found = load("qa", Settings, windows=False)
    assert (found.name, found.source, found.settings.snapshot_root, found.writes) == ("qa", path, "//files/runs-qa", True)


def test_a_pod_reads_the_process_s_environment_which_its_deployment_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_ENV", "prod")
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", "//files/runs-prod")
    found = load(None, Settings, windows=False)
    assert (found.name, found.source, found.settings.snapshot_root) == ("prod", None, "//files/runs-prod")


def test_an_environment_nothing_configures_is_refused_naming_the_user_s_file(no_basis_variables: Path) -> None:
    with pytest.raises(EnvError) as raised:
        load("dev", Settings, windows=False)
    assert f"there is no {no_basis_variables / 'dev.env'}, and the process's environment sets no BASIS_ settings" in str(raised.value)
    assert str(raised.value).endswith("with the names the repo's .env.example lists")


@pytest.mark.parametrize(("env", "flag", "writes"), [("dev", None, True), ("qa", None, True), ("prod", None, False), ("prod", "1", True), ("prod", "true", False)])
def test_prod_writes_only_where_the_process_sets_the_flag(monkeypatch: pytest.MonkeyPatch, env: str, flag: str | None, writes: bool) -> None:  # noqa: FBT001 -- a parametrized case
    user_file(env, "BASIS_SNAPSHOT_ROOT=runs\nBASIS_PROD_WRITES=1\n")  # a user's file cannot set it
    if flag is not None:
        monkeypatch.setenv(environments.PROD_WRITES, flag)
    found = load(env, Settings, windows=False)
    assert found.writes is writes
    if writes:
        found.refuse_writes("basis run positions --publish")
    else:
        with pytest.raises(EnvError, match=r"^basis run positions --publish would write to prod, which basis refuses unless the process sets BASIS_PROD_WRITES=1"):
            found.refuse_writes("basis run positions --publish")
