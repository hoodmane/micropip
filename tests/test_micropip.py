import io
import sys
import zipfile
from contextlib import contextmanager
from importlib.metadata import Distribution, PackageNotFoundError
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from pytest_pyodide import run_in_pyodide, spawn_web_server

try:
    from packaging.tags import Tag

    import micropip
except ImportError:
    pass

EMSCRIPTEN_VER = "3.1.14"


def _platform() -> str:
    # Vendored from pyodide_build.common
    version = EMSCRIPTEN_VER.replace(".", "_")
    return f"emscripten_{version}_wasm32"


PLATFORM = _platform()

cpver = f"cp{sys.version_info.major}{sys.version_info.minor}"


@pytest.fixture
def mock_platform(monkeypatch):
    monkeypatch.setenv("_PYTHON_HOST_PLATFORM", _platform())
    from micropip import _micropip

    monkeypatch.setattr(_micropip, "get_platform", _platform)


def _mock_importlib_version(name: str) -> str:
    dists = _mock_importlib_distributions()
    for dist in dists:
        if dist.name == name:
            return dist.version
    raise PackageNotFoundError(name)


WHEEL_BASE = None


@pytest.fixture
def wheel_base(monkeypatch):
    with TemporaryDirectory() as tmpdirname:
        global WHEEL_BASE
        WHEEL_BASE = Path(tmpdirname).absolute()
        import site

        monkeypatch.setattr(
            site, "getsitepackages", lambda: [WHEEL_BASE], raising=False
        )
        try:
            yield
        finally:
            WHEEL_BASE = None


def _mock_importlib_distributions():
    return (Distribution.at(p) for p in WHEEL_BASE.glob("*.dist-info"))  # type: ignore[union-attr]


@pytest.fixture
def mock_importlib(monkeypatch):
    from micropip import _micropip

    monkeypatch.setattr(_micropip, "importlib_version", _mock_importlib_version)
    monkeypatch.setattr(
        _micropip, "importlib_distributions", _mock_importlib_distributions
    )


class Wildcard:
    def __eq__(self, other):
        return True


def make_wheel_filename(name: str, version: str, platform: str = "generic") -> str:
    if platform == "generic":
        platform_str = "py3-none-any"
    elif platform == "emscripten":
        platform_str = f"{cpver}-{cpver}-{_platform()}"
    elif platform == "linux":
        platform_str = f"{cpver}-{cpver}-manylinux_2_31_x86_64"
    elif platform == "windows":
        platform_str = f"{cpver}-{cpver}-win_amd64"
    elif platform == "invalid":
        platform_str = f"{cpver}-{cpver}-invalid"
    else:
        platform_str = platform

    return f"{name.replace('-', '_').lower()}-{version}-{platform_str}.whl"


class mock_fetch_cls:
    def __init__(self):
        self.releases_map = {}
        self.metadata_map = {}
        self.top_level_map = {}

    def add_pkg_version(
        self,
        name: str,
        version: str = "1.0.0",
        *,
        requirements: list[str] | None = None,
        extras: dict[str, list[str]] | None = None,
        platform: str = "generic",
        top_level: list[str] | None = None,
    ) -> None:
        if requirements is None:
            requirements = []
        if extras is None:
            extras = {}
        if top_level is None:
            top_level = []
        if name not in self.releases_map:
            self.releases_map[name] = {"releases": {}}
        releases = self.releases_map[name]["releases"]
        filename = make_wheel_filename(name, version, platform)
        releases[version] = [
            {
                "filename": filename,
                "url": filename,
                "digests": {
                    "sha256": Wildcard(),
                },
            }
        ]
        metadata = [("Name", name), ("Version", version)] + [
            ("Requires-Dist", req) for req in requirements
        ]
        for extra, reqs in extras.items():
            metadata += [("Provides-Extra", extra)] + [
                ("Requires-Dist", f"{req}; extra == {extra!r}") for req in reqs
            ]
        self.metadata_map[filename] = metadata
        self.top_level_map[filename] = top_level

    async def _get_pypi_json(self, pkgname, kwargs):
        try:
            return self.releases_map[pkgname]
        except KeyError as e:
            raise ValueError(
                f"Can't fetch metadata for '{pkgname}' from PyPI. "
                "Please make sure you have entered a correct package name."
            ) from e

    async def _fetch_bytes(self, url, kwargs):
        from micropip._micropip import WheelInfo

        wheel_info = WheelInfo.from_url(url)
        version = wheel_info.version
        name = wheel_info.name
        filename = wheel_info.filename
        metadata = self.metadata_map[filename]
        metadata_str = "\n".join(": ".join(x) for x in metadata)
        toplevel = self.top_level_map[filename]
        toplevel_str = "\n".join(toplevel)

        metadata_dir = f"{name}-{version}.dist-info"

        tmp = io.BytesIO()
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:

            def write_file(filename, contents):
                archive.writestr(f"{metadata_dir}/{filename}", contents)

            write_file("METADATA", metadata_str)
            write_file("WHEEL", "Wheel-Version: 1.0")
            write_file("top_level.txt", toplevel_str)

        tmp.seek(0)

        return tmp


@pytest.fixture
def mock_fetch(monkeypatch, mock_importlib, wheel_base):
    pytest.importorskip("packaging")
    from micropip import _micropip

    result = mock_fetch_cls()
    monkeypatch.setattr(_micropip, "_get_pypi_json", result._get_pypi_json)
    monkeypatch.setattr(_micropip, "fetch_bytes", result._fetch_bytes)
    return result


@pytest.fixture(scope="module")
def wheel_path(tmp_path_factory):
    # Build a micropip wheel for testing
    import build
    from build.env import IsolatedEnvBuilder

    output_dir = tmp_path_factory.mktemp("wheel")

    with IsolatedEnvBuilder() as env:
        builder = build.ProjectBuilder(Path(__file__).parent.parent)
        builder.python_executable = env.executable
        builder.scripts_dir = env.scripts_dir
        env.install(builder.build_system_requires)
        builder.build("wheel", output_directory=output_dir)

    yield output_dir


@pytest.fixture
def selenium_standalone_micropip(selenium_standalone, wheel_path):
    """Import micropip before entering test so that global initialization of
    micropip doesn't count towards hiwire refcount.
    """

    wheel_dir = Path(wheel_path)
    wheel_files = list(wheel_dir.glob("*.whl"))

    if not wheel_files:
        pytest.exit("No wheel files found in wheel/ directory")

    wheel_file = wheel_files[0]
    with spawn_web_server(wheel_dir) as server:
        server_hostname, server_port, _ = server
        base_url = f"http://{server_hostname}:{server_port}/"
        selenium_standalone.run_js(
            f"""
            await pyodide.loadPackage("{base_url + wheel_file.name}");
            await pyodide.loadPackage(["packaging", "pyparsing"]);
            pyodide.runPython("import micropip");
            """
        )

    yield selenium_standalone


SNOWBALL_WHEEL = "snowballstemmer-2.0.0-py2.py3-none-any.whl"


def test_install_simple(selenium_standalone_micropip):
    selenium = selenium_standalone_micropip
    assert (
        selenium.run_js(
            """
            return await pyodide.runPythonAsync(`
                import os
                import micropip
                from pyodide import to_js
                # Package 'pyodide-micropip-test' has dependency on 'snowballstemmer'
                # It is used to test markers support
                await micropip.install('pyodide-micropip-test')
                import snowballstemmer
                stemmer = snowballstemmer.stemmer('english')
                to_js(stemmer.stemWords('go going goes gone'.split()))
            `);
            """
        )
        == ["go", "go", "goe", "gone"]
    )


@pytest.mark.parametrize(
    "path",
    [
        SNOWBALL_WHEEL,
        f"/{SNOWBALL_WHEEL}" f"a/{SNOWBALL_WHEEL}",
        f"/a/{SNOWBALL_WHEEL}",
        f"//a/{SNOWBALL_WHEEL}",
    ],
)
@pytest.mark.parametrize("protocol", ["https:", "file:", "emfs:", ""])
def test_parse_wheel_url1(protocol, path):
    pytest.importorskip("packaging")
    from micropip._micropip import WheelInfo

    url = protocol + path
    wheel = WheelInfo.from_url(url)
    assert wheel.name == "snowballstemmer"
    assert str(wheel.version) == "2.0.0"
    assert wheel.digests is None
    assert wheel.filename == SNOWBALL_WHEEL
    assert wheel.url == url
    assert wheel.tags == frozenset(
        {Tag("py2", "none", "any"), Tag("py3", "none", "any")}
    )


def test_parse_wheel_url2():
    from micropip._micropip import WheelInfo

    msg = r"Invalid wheel filename \(wrong number of parts\)"
    with pytest.raises(ValueError, match=msg):
        url = "https://a/snowballstemmer-2.0.0-py2.whl"
        WheelInfo.from_url(url)


def test_parse_wheel_url3():
    from micropip._micropip import WheelInfo

    url = "http://a/scikit_learn-0.22.2.post1-cp35-cp35m-macosx_10_9_intel.whl"
    wheel = WheelInfo.from_url(url)
    assert wheel.name == "scikit-learn"
    assert wheel.tags == frozenset({Tag("cp35", "cp35m", "macosx_10_9_intel")})


@pytest.mark.parametrize("base_url", ["'{base_url}'", "'.'"])
def test_install_custom_url(selenium_standalone_micropip, base_url):
    selenium = selenium_standalone_micropip

    with spawn_web_server(Path(__file__).parent / "dist") as server:
        server_hostname, server_port, _ = server
        base_url = f"http://{server_hostname}:{server_port}/"
        url = base_url + SNOWBALL_WHEEL

        selenium.run_js(
            f"""
            await pyodide.runPythonAsync(`
                import micropip
                await micropip.install('{url}')
                import snowballstemmer
            `);
            """
        )


@pytest.mark.xfail_browsers(chrome="node only", firefox="node only")
def test_install_file_protocol_node(selenium_standalone_micropip):
    selenium = selenium_standalone_micropip
    from conftest import DIST_PATH

    pyparsing_wheel_name = list(DIST_PATH.glob("pyparsing*.whl"))[0].name
    selenium.run_js(
        f"""
        await pyodide.runPythonAsync(`
            import micropip
            await micropip.install('file:{pyparsing_wheel_name}')
            import pyparsing
        `);
        """
    )


def create_transaction(Transaction):
    return Transaction(
        wheels=[],
        locked={},
        keep_going=True,
        deps=True,
        pre=False,
        pyodide_packages=[],
        failed=[],
        ctx={},
        ctx_extras=[],
        fetch_kwargs={},
    )


@pytest.mark.asyncio
async def test_add_requirement():
    pytest.importorskip("packaging")
    from micropip._micropip import Transaction

    with spawn_web_server(Path(__file__).parent / "dist") as server:
        server_hostname, server_port, _ = server
        base_url = f"http://{server_hostname}:{server_port}/"
        url = base_url + SNOWBALL_WHEEL

        transaction = create_transaction(Transaction)
        await transaction.add_requirement(url)

    wheel = transaction.wheels[0]
    assert wheel.name == "snowballstemmer"
    assert str(wheel.version) == "2.0.0"
    assert wheel.filename == SNOWBALL_WHEEL
    assert wheel.url == url
    assert wheel.tags == frozenset(
        {Tag("py2", "none", "any"), Tag("py3", "none", "any")}
    )


@pytest.mark.asyncio
async def test_add_requirement_marker(mock_importlib, wheel_base):
    pytest.importorskip("packaging")
    from micropip._micropip import Transaction

    transaction = create_transaction(Transaction)

    await transaction.gather_requirements(
        [
            "werkzeug",
            'contextvars ; python_version < "3.7"',
            'aiocontextvars ; python_version < "3.7"',
            "numpy ; extra == 'full'",
            "zarr ; extra == 'full'",
            "numpy ; extra == 'jupyter'",
            "ipykernel ; extra == 'jupyter'",
            "numpy ; extra == 'socketio'",
            "python-socketio[client] ; extra == 'socketio'",
        ],
    )

    non_targets = [
        "contextvars",
        "aiocontextvars",
        "numpy",
        "zarr",
        "ipykernel",
        "python-socketio",
    ]

    wheel_files = [wheel.name for wheel in transaction.wheels]
    assert "werkzeug" in wheel_files
    for t in non_targets:
        assert t not in wheel_files


@pytest.mark.asyncio
async def test_add_requirement_query_url(mock_importlib, wheel_base, monkeypatch):
    pytest.importorskip("packaging")
    from micropip._micropip import Transaction

    async def mock_add_wheel(self, wheel, extras):
        self.mock_wheel = wheel

    monkeypatch.setattr(Transaction, "add_wheel", mock_add_wheel)

    transaction = create_transaction(Transaction)
    await transaction.add_requirement(f"{SNOWBALL_WHEEL}?b=1")
    wheel = transaction.mock_wheel
    assert wheel.name == "snowballstemmer"
    assert wheel.filename == SNOWBALL_WHEEL  # without the query params


@pytest.mark.asyncio
async def test_package_with_extra(mock_fetch):
    mock_fetch.add_pkg_version("depa")
    mock_fetch.add_pkg_version("depb")
    mock_fetch.add_pkg_version("pkga", extras={"opt_feature": ["depa"]})
    mock_fetch.add_pkg_version("pkgb", extras={"opt_feature": ["depb"]})

    await micropip.install(["pkga[opt_feature]", "pkgb"])

    pkg_list = micropip.list()

    assert "pkga" in pkg_list
    assert "depa" in pkg_list

    assert "pkgb" in pkg_list
    assert "depb" not in pkg_list


@pytest.mark.asyncio
async def test_package_with_extra_all(mock_fetch):

    mock_fetch.add_pkg_version("depa")
    mock_fetch.add_pkg_version("depb")
    mock_fetch.add_pkg_version("depc")
    mock_fetch.add_pkg_version("depd")

    mock_fetch.add_pkg_version("pkga", extras={"all": ["depa", "depb"]})
    mock_fetch.add_pkg_version(
        "pkgb", extras={"opt_feature": ["depc"], "all": ["depc", "depd"]}
    )

    await micropip.install(["pkga[all]", "pkgb[opt_feature]"])

    pkg_list = micropip.list()
    assert "depa" in pkg_list
    assert "depb" in pkg_list

    assert "depc" in pkg_list
    assert "depd" not in pkg_list


@pytest.mark.parametrize("transitive_req", [True, False])
@pytest.mark.asyncio
async def test_package_with_extra_transitive(
    mock_fetch, transitive_req, mock_importlib
):
    mock_fetch.add_pkg_version("depb")

    pkga_optional_dep = "depa[opt_feature]" if transitive_req else "depa"
    mock_fetch.add_pkg_version("depa", extras={"opt_feature": ["depb"]})
    mock_fetch.add_pkg_version("pkga", extras={"opt_feature": [pkga_optional_dep]})

    await micropip.install(["pkga[opt_feature]"])
    pkg_list = micropip.list()
    assert "depa" in pkg_list
    if transitive_req:
        assert "depb" in pkg_list
    else:
        assert "depb" not in pkg_list


def _pypi_metadata(package, versions_to_tags):
    # Build package release metadata as would be returned from
    # https://pypi.org/pypi/{pkgname}/json
    #
    # `package` is a string containing the package name as
    # it would appear in a wheel file name.
    #
    # `versions` is a mapping with version strings as
    # keys and iterables of tag strings as values.
    releases = {}
    for version, tags in versions_to_tags.items():
        release = []
        for tag in tags:
            wheel_name = f"{package}-{version}-{tag}-none-any.whl"
            wheel_info = {
                "filename": wheel_name,
                "url": wheel_name,
                "digests": None,
            }
            release.append(wheel_info)
        releases[version] = release
    return {"releases": releases}


def test_last_version_from_pypi():
    pytest.importorskip("packaging")
    from packaging.requirements import Requirement

    from micropip._micropip import find_wheel

    requirement = Requirement("dummy_module")
    versions = ["0.0.1", "0.15.5", "0.9.1"]

    metadata = _pypi_metadata("dummy_module", {v: ["py3"] for v in versions})

    # get version number from find_wheel
    wheel = find_wheel(metadata, requirement)

    assert str(wheel.version) == "0.15.5"


_best_tag_test_cases = (
    "package, version, incompatible_tags, compatible_tags",
    # Tests assume that `compatible_tags` is sorted from least to most compatible:
    [
        # Common modern case (pure Python 3-only wheel):
        ("hypothesis", "6.60.0", [], ["py3"]),
        # Common historical case (pure Python 2-or-3 wheel):
        ("attrs", "22.1.0", [], ["py2.py3"]),
        # Still simple, less common (separate Python 2 and 3 wheels):
        ("raise", "1.1.9", ["py2"], ["py3"]),
        # More complicated, rarer cases:
        ("compose", "1.4.8", [], ["py2.py30", "py35", "py38"]),
        ("with_as_a_function", "1.0.1", ["py20", "py25"], ["py26.py3"]),
        ("with_as_a_function", "1.1.0", ["py22", "py25"], ["py26.py30", "py33"]),
    ],
)


@pytest.mark.parametrize(*_best_tag_test_cases)
def test_best_tag_from_pypi(package, version, incompatible_tags, compatible_tags):
    pytest.importorskip("packaging")
    from packaging.requirements import Requirement

    from micropip._micropip import find_wheel

    requirement = Requirement(package)
    tags = incompatible_tags + compatible_tags

    metadata = _pypi_metadata(package, {version: tags})

    wheel = find_wheel(metadata, requirement)

    best_tag = tags[-1].split(".")[-1] + "-none-any"
    assert best_tag in set(map(str, wheel.tags))


# A newer version with a compatible wheel has higher precedence
# than an older version with a more precisely compatible wheel.
# This test verifies that we didn't break that corner case:
@pytest.mark.parametrize(
    "package, old_version, old_tags, new_version, new_tags",
    [
        ("compose", "1.1.1", ["py2.py3"], "1.2.0", ["py2.py30", "py35", "py38"]),
        (
            "with_as_a_function",
            "1.0.1",
            ["py20", "py25", "py26.py3"],
            "1.1.0",
            ["py22", "py25", "py26.py30", "py33"],
        ),
    ],
)
def test_last_version_and_best_tag_from_pypi(
    package, old_version, new_version, old_tags, new_tags
):
    pytest.importorskip("packaging")
    from packaging.requirements import Requirement

    from micropip._micropip import find_wheel

    requirement = Requirement(package)

    metadata = _pypi_metadata(
        package,
        {old_version: old_tags, new_version: new_tags},
    )

    wheel = find_wheel(metadata, requirement)

    assert str(wheel.version) == new_version


@pytest.mark.asyncio
async def test_install_non_pure_python_wheel():
    pytest.importorskip("packaging")
    from micropip._micropip import Transaction

    msg = "Wheel platform 'macosx_10_9_intel' is not compatible with Pyodide's platform"
    with pytest.raises(ValueError, match=msg):
        url = "http://a/scikit_learn-0.22.2.post1-cp35-cp35m-macosx_10_9_intel.whl"
        transaction = create_transaction(Transaction)
        await transaction.add_requirement(url)


def test_install_different_version(selenium_standalone_micropip):
    selenium = selenium_standalone_micropip
    selenium.run_js(
        """
        await pyodide.runPythonAsync(`
            import micropip
            await micropip.install(
                "https://files.pythonhosted.org/packages/89/06/2c2d3034b4d6bf22f2a4ae546d16925898658a33b4400cfb7e2c1e2871a3/pytz-2020.5-py2.py3-none-any.whl"
            );
        `);
        """
    )
    selenium.run_js(
        """
        await pyodide.runPythonAsync(`
            import pytz
            assert pytz.__version__ == "2020.5"
        `);
        """
    )


def test_install_different_version2(selenium_standalone_micropip):
    selenium = selenium_standalone_micropip
    selenium.run_js(
        """
        await pyodide.runPythonAsync(`
            import micropip
            await micropip.install(
                "pytz == 2020.5"
            );
        `);
        """
    )
    selenium.run_js(
        """
        await pyodide.runPythonAsync(`
            import pytz
            assert pytz.__version__ == "2020.5"
        `);
        """
    )


@pytest.mark.parametrize("jinja2", ["jinja2", "Jinja2"])
def test_install_mixed_case2(selenium_standalone_micropip, jinja2):
    selenium = selenium_standalone_micropip
    selenium.run_js(
        f"""
        await pyodide.loadPackage("micropip");
        await pyodide.runPythonAsync(`
            import micropip
            await micropip.install("{jinja2}")
            import jinja2
        `);
        """
    )


@pytest.mark.asyncio
async def test_install_keep_going(mock_fetch: mock_fetch_cls) -> None:
    dummy = "dummy"
    dep1 = "dep1"
    dep2 = "dep2"
    mock_fetch.add_pkg_version(dummy, requirements=[dep1, dep2])
    mock_fetch.add_pkg_version(dep1, platform="invalid")
    mock_fetch.add_pkg_version(dep2, platform="invalid")

    # report order is non-deterministic
    msg = f"({dep1}|{dep2}).*({dep2}|{dep1})"
    with pytest.raises(ValueError, match=msg):
        await micropip.install(dummy, keep_going=True)


@pytest.mark.asyncio
async def test_install_version_compare_prerelease(mock_fetch: mock_fetch_cls) -> None:
    dummy = "dummy"
    version_old = "3.2.0"
    version_new = "3.2.1a1"

    mock_fetch.add_pkg_version(dummy, version_old)
    mock_fetch.add_pkg_version(dummy, version_new)

    await micropip.install(f"{dummy}=={version_new}")
    await micropip.install(f"{dummy}>={version_old}")

    installed_pkgs = micropip.list()
    # Older version should not be installed
    assert installed_pkgs[dummy].version == version_new


@pytest.mark.asyncio
async def test_install_no_deps(mock_fetch: mock_fetch_cls) -> None:
    dummy = "dummy"
    dep = "dep"
    mock_fetch.add_pkg_version(dummy, requirements=[dep])
    mock_fetch.add_pkg_version(dep)

    await micropip.install(dummy, deps=False)

    assert dummy in micropip.list()
    assert dep not in micropip.list()


@pytest.mark.asyncio
@pytest.mark.parametrize("pre", [True, False])
async def test_install_pre(
    mock_fetch: mock_fetch_cls,
    pre: bool,
) -> None:
    dummy = "dummy"
    version_alpha = "2.0.1a1"
    version_stable = "1.0.0"

    version_should_select = version_alpha if pre else version_stable

    mock_fetch.add_pkg_version(dummy, version_stable)
    mock_fetch.add_pkg_version(dummy, version_alpha)
    await micropip.install(dummy, pre=pre)
    assert micropip.list()[dummy].version == version_should_select


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::Warning")
@pytest.mark.parametrize("version_invalid", ["1.2.3-1", "2.3.1-post1", "3.2.1-pre1"])
async def test_install_version_invalid_pep440(
    mock_fetch: mock_fetch_cls,
    version_invalid: str,
) -> None:
    # Micropip should skip package versions which do not follow PEP 440.
    #
    #     [N!]N(.N)*[{a|b|rc}N][.postN][.devN]
    #

    dummy = "dummy"
    version_stable = "1.0.0"

    mock_fetch.add_pkg_version(dummy, version_stable)
    mock_fetch.add_pkg_version(dummy, version_invalid)
    await micropip.install(dummy)
    assert micropip.list()[dummy].version == version_stable


@pytest.mark.asyncio
async def test_fetch_wheel_fail(monkeypatch, wheel_base):
    pytest.importorskip("packaging")
    from micropip import _micropip

    def _mock_fetch_bytes(arg, *args, **kwargs):
        raise OSError(f"Request for {arg} failed with status 404: Not Found")

    monkeypatch.setattr(_micropip, "fetch_bytes", _mock_fetch_bytes)

    msg = "Access-Control-Allow-Origin"
    with pytest.raises(ValueError, match=msg):
        await _micropip.install("htps://x.com/xxx-1.0.0-py3-none-any.whl")


@pytest.mark.asyncio
async def test_list_pypi_package(mock_fetch: mock_fetch_cls) -> None:
    dummy = "dummy"
    mock_fetch.add_pkg_version(dummy)

    await micropip.install(dummy)
    pkg_list = micropip.list()
    assert dummy in pkg_list
    assert pkg_list[dummy].source.lower() == "pypi"


@pytest.mark.asyncio
async def test_list_wheel_package(mock_fetch: mock_fetch_cls) -> None:
    dummy = "dummy"
    mock_fetch.add_pkg_version(dummy)
    dummy_url = f"https://dummy.com/{dummy}-1.0.0-py3-none-any.whl"

    await micropip.install(dummy_url)

    pkg_list = micropip.list()
    assert dummy in pkg_list
    assert pkg_list[dummy].source.lower() == dummy_url


@pytest.mark.asyncio
async def test_list_wheel_name_mismatch(mock_fetch: mock_fetch_cls) -> None:
    dummy_pkg_name = "dummy-Dummy"
    mock_fetch.add_pkg_version(dummy_pkg_name)
    dummy_url = "https://dummy.com/dummy_dummy-1.0.0-py3-none-any.whl"

    await micropip.install(dummy_url)

    pkg_list = micropip.list()
    assert dummy_pkg_name in pkg_list
    assert pkg_list[dummy_pkg_name].source.lower() == dummy_url


def test_list_load_package_from_url(selenium_standalone_micropip):
    with spawn_web_server(Path(__file__).parent / "dist") as server:
        server_hostname, server_port, _ = server
        base_url = f"http://{server_hostname}:{server_port}/"
        url = base_url + SNOWBALL_WHEEL

        selenium = selenium_standalone_micropip
        selenium.run_js(
            f"""
            await pyodide.loadPackage({url!r});
            await pyodide.runPythonAsync(`
                import micropip
                assert "snowballstemmer" in micropip.list()
            `);
            """
        )


def test_list_pyodide_package(selenium_standalone_micropip):
    selenium = selenium_standalone_micropip
    selenium.run_js(
        """
        await pyodide.runPythonAsync(`
            import micropip
            await micropip.install(
                "regex"
            );
        `);
        """
    )
    selenium.run_js(
        """
        await pyodide.runPythonAsync(`
            import micropip
            pkgs = micropip.list()
            assert "regex" in pkgs
            assert pkgs["regex"].source.lower() == "pyodide"
        `);
        """
    )


def test_list_loaded_from_js(selenium_standalone_micropip):
    selenium = selenium_standalone_micropip
    selenium.run_js(
        """
        await pyodide.loadPackage("regex");
        await pyodide.runPythonAsync(`
            import micropip
            pkgs = micropip.list()
            assert "regex" in pkgs
            assert pkgs["regex"].source.lower() == "pyodide"
        `);
        """
    )


@pytest.mark.skip_refcount_check
@run_in_pyodide(packages=["micropip"])
async def test_install_with_credentials(selenium):
    import json
    from unittest.mock import MagicMock, patch

    import micropip

    fetch_response_mock = MagicMock()

    async def myfunc():
        return json.dumps(dict())

    fetch_response_mock.string.side_effect = myfunc

    @patch("micropip._compat_in_pyodide.pyfetch", return_value=fetch_response_mock)
    async def call_micropip_install(pyfetch_mock):
        try:
            await micropip.install("pyodide-micropip-test", credentials="include")
        except BaseException:
            # The above will raise an exception as the mock data is garbage
            # but it is sufficient for this test
            pass
        pyfetch_mock.assert_called_with(
            "https://pypi.org/pypi/pyodide-micropip-test/json", credentials="include"
        )

    await call_micropip_install()


@pytest.mark.asyncio
async def test_load_binary_wheel1(
    mock_fetch: mock_fetch_cls, mock_importlib: None, mock_platform: None
) -> None:
    dummy = "dummy"
    mock_fetch.add_pkg_version(dummy, platform="emscripten")
    await micropip.install(dummy)


@pytest.mark.skip_refcount_check
@run_in_pyodide(packages=["micropip"])
async def test_load_binary_wheel2(selenium):
    from pyodide_js._api import repodata_packages

    import micropip

    await micropip.install(repodata_packages.regex.file_name)
    import regex  # noqa: F401


@pytest.mark.asyncio
async def test_freeze(mock_fetch: mock_fetch_cls) -> None:
    dummy = "dummy"
    dep1 = "dep1"
    dep2 = "dep2"
    toplevel = [["abc", "def", "geh"], ["c", "h", "i"], ["a12", "b13"]]

    mock_fetch.add_pkg_version(dummy, requirements=[dep1, dep2], top_level=toplevel[0])
    mock_fetch.add_pkg_version(dep1, top_level=toplevel[1])
    mock_fetch.add_pkg_version(dep2, top_level=toplevel[2])

    await micropip.install(dummy)

    import json

    lockfile = json.loads(micropip.freeze())

    pkg_metadata = lockfile["packages"][dummy]
    dep1_metadata = lockfile["packages"][dep1]
    dep2_metadata = lockfile["packages"][dep2]
    assert pkg_metadata["depends"] == [dep1, dep2]
    assert dep1_metadata["depends"] == []
    assert dep2_metadata["depends"] == []
    assert pkg_metadata["imports"] == toplevel[0]
    assert dep1_metadata["imports"] == toplevel[1]
    assert dep2_metadata["imports"] == toplevel[2]


def test_emfs(selenium_standalone_micropip):
    with spawn_web_server(Path(__file__).parent / "dist") as server:
        server_hostname, server_port, _ = server
        url = f"http://{server_hostname}:{server_port}/"

        @run_in_pyodide(packages=["micropip"])
        async def run_test(selenium, url, wheel_name):
            from pyodide.http import pyfetch

            import micropip

            resp = await pyfetch(url + wheel_name)
            await resp._into_file(open(wheel_name, "wb"))
            await micropip.install("emfs:" + wheel_name)
            import snowballstemmer

            stemmer = snowballstemmer.stemmer("english")
            assert stemmer.stemWords("go going goes gone".split()) == [
                "go",
                "go",
                "goe",
                "gone",
            ]

        run_test(selenium_standalone_micropip, url, SNOWBALL_WHEEL)


@contextmanager
def does_not_raise():
    yield


def raiseValueError(msg):
    return pytest.raises(ValueError, match=msg)


@pytest.mark.parametrize(
    "interp, abi, arch,ctx",
    [
        (
            "cp35",
            "cp35m",
            "macosx_10_9_intel",
            raiseValueError(
                f"Wheel platform 'macosx_10_9_intel' .* Pyodide's platform '{PLATFORM}'"
            ),
        ),
        (
            "cp35",
            "cp35m",
            "emscripten_2_0_27_wasm32",
            raiseValueError(
                f"Emscripten v2.0.27 but Pyodide was built with Emscripten v{EMSCRIPTEN_VER}"
            ),
        ),
        (
            "cp35",
            "cp35m",
            PLATFORM,
            raiseValueError(
                f"Wheel abi 'cp35m' .* Supported abis are 'abi3' and '{cpver}'."
            ),
        ),
        ("cp35", "abi3", PLATFORM, does_not_raise()),
        (cpver, "abi3", PLATFORM, does_not_raise()),
        (cpver, cpver, PLATFORM, does_not_raise()),
        (
            "cp35",
            cpver,
            PLATFORM,
            raiseValueError("Wheel interpreter version 'cp35' is not supported."),
        ),
        (
            "cp391",
            "abi3",
            PLATFORM,
            raiseValueError("Wheel interpreter version 'cp391' is not supported."),
        ),
    ],
)
def test_check_compatible(mock_platform, interp, abi, arch, ctx):
    from micropip._micropip import WheelInfo

    pkg = "scikit_learn-0.22.2.post1"
    wheel_name = f"{pkg}-{interp}-{abi}-{arch}.whl"
    with ctx:
        WheelInfo.from_url(wheel_name).check_compatible()


@pytest.mark.parametrize(*_best_tag_test_cases)
def test_best_compatible_tag(package, version, incompatible_tags, compatible_tags):
    from micropip._micropip import WheelInfo

    for tag in incompatible_tags:
        wheel_name = f"{package}-{version}-{tag}-none-any.whl"
        wheel = WheelInfo.from_url(wheel_name)
        assert wheel.best_compatible_tag_index() is None

    wheels = []
    for tag in compatible_tags:
        wheel_name = f"{package}-{version}-{tag}-none-any.whl"
        wheel = WheelInfo.from_url(wheel_name)
        wheels.append(wheel)

    sorted_wheels = sorted(wheels, key=WheelInfo.best_compatible_tag_index)
    sorted_wheels.reverse()
    assert sorted_wheels == wheels


@run_in_pyodide()
def test_persistent_mock_pyodide(selenium_standalone_micropip):  #
    import sys
    from importlib.metadata import version as importlib_version
    from io import StringIO

    from micropip._micropip import add_mock_package

    capture_stdio = StringIO()

    sys.stdout = capture_stdio
    add_mock_package("test_1", "1.0.0", persistent=True)
    add_mock_package(
        "test_2",
        "1.2.0",
        modules={
            "t1": "print('hi from t1')",
            "t2": """
        def fn():
            print("Hello from fn")
        """,
        },
        persistent=True,
    )
    import test_1

    dir(test_1)

    import t1

    dir(t1)
    import t2

    dir(t2)

    t2.fn()
    assert capture_stdio.getvalue().find("Hello from fn") != -1
    assert capture_stdio.getvalue().find("hi from t1") != -1
    assert importlib_version("test_1") == "1.0.0"


def test_persistent_mock(monkeypatch, capsys, tmp_path):
    import site
    from importlib.metadata import version as importlib_version

    from micropip._micropip import add_mock_package

    def _getusersitepackages():
        return tmp_path

    monkeypatch.setattr(site, "getusersitepackages", _getusersitepackages)
    monkeypatch.setattr(sys, "path", [tmp_path])
    add_mock_package("test_1", "1.0.0")
    add_mock_package(
        "test_2",
        "1.2.0",
        modules={
            "t1": "print('hi from t1')",
            "t2": """
        def fn():
            print("Hello from fn")
        """,
        },
    )
    import t1

    dir(t1)
    import t2

    dir(t2)
    import test_1

    dir(test_1)

    t2.fn()
    assert importlib_version("test_2") == "1.2.0"
    captured = capsys.readouterr()
    assert captured.out.find("hi from t1") != -1
    assert captured.out.find("Hello from fn") != -1


def test_memory_mock():
    from micropip import _micropip

    def mod_init(module):
        module.__dict__["add2"] = lambda x: x + 2

    _micropip.add_mock_package(
        "micropip_test_bob",
        "1.0.0",
        modules={
            "micropip_bob_mod": "print('hi from bob')",
            "micropip_bob_mod.fn": mod_init,
        },
        persistent=False,
    )
    import importlib

    import micropip_bob_mod

    dir(micropip_bob_mod)

    import micropip_bob_mod.fn

    assert micropip_bob_mod.fn.add2(5) == 7

    found_bob = False
    for d in importlib.metadata.distributions():
        if d.name == "micropip_test_bob":
            found_bob = True
    assert found_bob is True
    assert (
        importlib.metadata.distribution("micropip_test_bob").name == "micropip_test_bob"
    )
    # check package removes okay
    _micropip.remove_mock_package("micropip_test_bob")
    del micropip_bob_mod
    with pytest.raises(ImportError):
        import micropip_bob_mod

        dir(micropip_bob_mod)


@run_in_pyodide()
def test_memory_mock_pyodide(selenium_standalone_micropip):
    import pytest

    from micropip import _micropip

    def mod_init(module):
        module.__dict__["add2"] = lambda x: x + 2

    _micropip.add_mock_package(
        "micropip_test_bob",
        "1.0.0",
        modules={
            "micropip_bob_mod": "print('hi from bob')",
            "micropip_bob_mod.fn": mod_init,
        },
        persistent=False,
    )
    import importlib

    import micropip_bob_mod

    dir(micropip_bob_mod)

    import micropip_bob_mod.fn

    assert micropip_bob_mod.fn.add2(5) == 7

    found_bob = False
    for d in importlib.metadata.distributions():
        if d.name == "micropip_test_bob":
            found_bob = True
    assert found_bob is True
    assert (
        importlib.metadata.distribution("micropip_test_bob").name == "micropip_test_bob"
    )
    # check package removes okay
    _micropip.remove_mock_package("micropip_test_bob")
    del micropip_bob_mod
    with pytest.raises(ImportError):
        import micropip_bob_mod

        dir(micropip_bob_mod)
