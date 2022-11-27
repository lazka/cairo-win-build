import hashlib
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import typing as T
import urllib.parse
import urllib.request
import argparse

from pathlib import Path

DEFAULT_CAIRO_VERSION = "1.17.6"
DOWNLOAD_URL_CAIRO = "https://gitlab.freedesktop.org/cairo/cairo/-/archive/{CAIRO_VERSION}/{CAIRO_VERSION}.tar.gz"

DEFAULT_PKGCONF_VERSION = "1.8.0"
SHA256SUM_CAIRO = "49f88d58cf4cf2252dbf0c7e7e42d62812f7aabdee4a0c0793d509a6ce1be266"
DOWNLOAD_URL_PKGCONF = (
    "https://distfiles.dereferenced.org/pkgconf/pkgconf-{PKGCONF_VERSION}.tar.gz"
)
SHA256SUM_PKGCONF = "d7b6fdb522d81c11f5a0e0a0629a9f5480809ec90e595058674c1517822dfb8c"
DEFAULT_PREFIX = Path("C:/prefix") if sys.platform == "win32" else sys.prefix

DEFAULT_PATCH_EXE = shutil.which("patch")

log = logging.getLogger(__name__)
ENVIRON = os.environ.copy()


def get_python_arch() -> int:
    return struct.calcsize("P") * 8


def check_sha256(filepath: Path, hash: str) -> None:
    def get_sha256_from_file(file_path: Path) -> str:
        sha256 = hashlib.sha256()
        BUF_SIZE = 65536  # lets read stuff in 64kb chunks!
        with open(file_path, "rb") as f:
            while True:
                data = f.read(BUF_SIZE)
                if not data:
                    break
                sha256.update(data)
        return sha256.hexdigest()

    file_sha = get_sha256_from_file(filepath)
    if get_sha256_from_file(filepath) != hash:
        raise Exception(
            f"The downloaded file does not match the expected hash.  {filepath} was "
            f"expected to have {hash} but it has {file_sha}"
        )
    log.info("File hash matched.")


def run_command(
    args: T.List[str],
    env: T.Dict[str, str] = ENVIRON,
    cwd: T.Optional[T.Union[str, Path]] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    kwargs = {"check": check}
    if env:
        kwargs["env"] = env
    if cwd:
        kwargs["cwd"] = cwd

    return subprocess.run(args, **kwargs)


def get_meson_executable(build_dir) -> Path:
    def create_venv_and_install_meson(venv_location: Path) -> None:
        log.info("Creating a venv at %s", venv_location.absolute())
        run_command(
            [
                sys.executable,
                "-m",
                "venv",
                os.fspath(venv_location),
            ]
        )
        log.info("Installing meson and ninja using pip")
        if (venv_location / "Scripts").is_dir():
            python = venv_location / "Scripts" / "python"
        else:
            python = venv_location / "bin" / "python"
        run_command(
            [
                os.fspath(python),
                "-m",
                "pip",
                "install",
                "ninja",
                "meson",
            ],
            env=ENVIRON,
        )

    log.info("Checking if meson and ninja are installed")
    meson = shutil.which("meson")
    ninja = shutil.which("ninja")
    if meson is None or ninja is None:
        log.warning("Meson or Ninja isn't installed. Installing using an Venv.")
        venv_location = build_dir / "meson_venv"
        ENVIRON[
            "PATH"
        ] = f"{(venv_location / 'Scripts').absolute()}{os.pathsep}{(venv_location / 'bin').absolute()}{os.pathsep}{ENVIRON['PATH']}"  # noqa
        if not venv_location.exists():
            create_venv_and_install_meson(venv_location)
        else:
            log.info("Venv already exists. Checking if it is usable.")
            ninja = shutil.which("ninja", path=ENVIRON["PATH"])
            meson = shutil.which("meson", path=ENVIRON["PATH"])
            if meson is None or ninja is None:
                log.info("Creating new venv.")
                create_venv_and_install_meson(venv_location)
    ninja = shutil.which("ninja", path=ENVIRON["PATH"])
    meson = shutil.which("meson", path=ENVIRON["PATH"])
    log.info("Found meson at %s", meson)
    log.info("Found ninja at %s", ninja)
    return meson


def run_meson(meson_args, **kwargs):
    log.info("Running meson with arguments: %s", " ".join(meson_args))
    run_command(meson_args, **kwargs)


def apply_patch(patch_location: Path, cwd: Path):
    log.info(f"Applying patch: {patch_location} (cwd: {cwd.absolute()})")
    patch_exe = DEFAULT_PATCH_EXE
    if DEFAULT_PATCH_EXE is None:
        if sys.platform != "win32":
            raise Exception("'patch' executable not found")
        log.warn("'patch.exe' not found in PATH, trying default from msys2")
        # by default msys2 is installing in C:\msys64\
        # so try `C:\msys64\usr\bin\patch.exe`
        patch_exe = r"C:\msys64\usr\bin\patch.exe"
        # fail if this doesn't exists, more elegant soln exists but well...
        assert os.path.exists(patch_exe), "Can't find 'patch.exe'"
    run_command(
        [
            patch_exe,
            "-p1",
            "-i",
            os.fspath(patch_location),
        ],
        cwd=cwd,
    )


# Copied from
# https://github.com/mesonbuild/meson/blob/928078982c8643bffd95a8da06a1b4494fe87e2b/mesonbuild/mesonlib/vsenv.py
def setup_vs(arch: int = 64) -> bool:
    bat_template = textwrap.dedent(
        """@ECHO OFF
        call "{}"
        ECHO {}
        SET
    """
    )
    if not sys.platform == "win32":
        return False
    if os.environ.get("OSTYPE") == "cygwin":
        return False
    if "Visual Studio" in os.environ["PATH"]:
        return False
    # VSINSTALL is set when running setvars from a Visual Studio installation
    # Tested with Visual Studio 2012 and 2017
    if "VSINSTALLDIR" in os.environ:
        return False
    # Check explicitly for cl when on Windows
    if "gcc" in sys.version.lower():
        if shutil.which("cl.exe"):
            return False
        if shutil.which("cc"):
            return False
        if shutil.which("gcc"):
            return False
        if shutil.which("clang"):
            return False
        if shutil.which("clang-cl"):
            return False

    root = os.environ.get("ProgramFiles(x86)") or os.environ.get("ProgramFiles")
    bat_locator_bin = Path(root, "Microsoft Visual Studio/Installer/vswhere.exe")
    if not bat_locator_bin.exists():
        raise Exception(f"Could not find {bat_locator_bin}")
    bat_json = subprocess.check_output(
        [
            str(bat_locator_bin),
            "-latest",
            "-prerelease",
            "-requiresAny",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-products",
            "*",
            "-utf8",
            "-format",
            "json",
        ]
    )
    bat_info = json.loads(bat_json)
    if not bat_info:
        # VS installer instelled but not VS itself maybe?
        raise Exception("Could not parse vswhere.exe output")
    bat_root = Path(bat_info[0]["installationPath"])
    bat_path = bat_root / f"VC/Auxiliary/Build/vcvars{arch}.bat"
    if not bat_path.exists():
        raise Exception(f"Could not find {bat_path}")

    bat_separator = "---SPLIT---"
    bat_contents = bat_template.format(bat_path, bat_separator)
    bat_file = tempfile.NamedTemporaryFile(
        "w", suffix=".bat", encoding="utf-8", delete=False
    )
    bat_file.write(bat_contents)
    bat_file.flush()
    bat_file.close()
    bat_output = subprocess.check_output(
        bat_file.name,
        universal_newlines=True,
    )
    bat_lines = bat_output.split("\n")
    bat_separator_seen = False
    for bat_line in bat_lines:
        if bat_line == bat_separator:
            bat_separator_seen = True
            continue
        if not bat_separator_seen:
            continue
        if not bat_line:
            continue
        k, v = bat_line.split("=", 1)
        if k.lower() == "path":
            k = "PATH"
        ENVIRON[k] = v
    return True


def download_and_extract(
    url: str, destdir: Path, shasum: str, check_file_hash: bool = True
):
    tararchive = Path(
        destdir, urllib.parse.urlsplit(url).path.split("/")[-1]
    ).absolute()
    if not tararchive.exists():
        log.info("Downloading %s to %s", url, tararchive)
        urllib.request.urlretrieve(
            url,
            tararchive,
        )
    if check_file_hash:
        check_sha256(tararchive, shasum)

    extract_dir = Path(destdir, ".".join(tararchive.name.split(".")[:-2])).absolute()
    if not extract_dir.exists() or not extract_dir.is_dir():
        log.info("Extracting archive")
        with tarfile.open(tararchive) as tar:
            tar.extractall(destdir)
        if not extract_dir.exists():
            shutil.move(
                list(Path(destdir).glob("*-*-*"))[0],
                extract_dir,
            )
    return extract_dir


def build_pkgconf(
    pkgconf_version: str = DEFAULT_PKGCONF_VERSION,
    arch: int = get_python_arch(),
    build_dir: T.Optional[Path] = None,
    check_file_hash: bool = True,
    file_hash_sha256: str = SHA256SUM_PKGCONF,
    prefix: Path = None,
    build_type: str = "static",
):
    log.info("Building Pkgconf")
    if build_dir is None:
        build_dir = Path(f"./build-pkgconf-v{pkgconf_version}-x{arch}")
    if build_dir.exists():
        log.info("%s exists. Skipping build.", build_dir.absolute())
        return
    build_dir.mkdir()
    log.info("Using %s as build directory.", build_dir.absolute())

    if prefix is None:
        if sys.platform == "win32":
            prefix = Path(rf"C:\build-x{arch}")
        else:
            prefix = Path(f"~/build-x{arch}")
    log.info("Using %s as prefix", prefix)

    setup_vs(arch)

    root_dir = download_and_extract(
        DOWNLOAD_URL_PKGCONF.format(PKGCONF_VERSION=pkgconf_version),
        build_dir,
        file_hash_sha256,
        check_file_hash,
    )

    meson = get_meson_executable(build_dir)

    meson_build_dir = (root_dir / f"build-x{arch}").absolute()
    if meson_build_dir.exists():
        shutil.rmtree(meson_build_dir)

    log.info("Configuring using Meson...")
    run_meson(
        [
            meson,
            "setup",
            os.fspath(meson_build_dir),
            f"--default-library={build_type}",
            f"--prefix={prefix}",
            "--buildtype=release",
            "-Dtests=false",  # We don't need tests
        ],
        cwd=root_dir,
        env=ENVIRON,
    )

    log.info("Compiling now...")
    run_meson(
        [meson, "compile", "-C", os.fspath(meson_build_dir)],
        cwd=root_dir,
    )

    log.info("Installing Pkgconf...")
    run_meson(
        [
            meson,
            "install",
            "--no-rebuild",
            "-C",
            os.fspath(meson_build_dir),
        ]
    )

    log.info("Sucessfully build Pkgconf")


def build_cairo(
    cairo_version: str = DEFAULT_CAIRO_VERSION,
    arch: int = get_python_arch(),
    build_dir: T.Optional[Path] = None,
    check_file_hash: bool = True,
    file_hash_sha256: str = SHA256SUM_CAIRO,
    prefix: Path = None,
    build_type: str = "static",
):
    log.info("Buidling Cairo")
    if build_dir is None:
        build_dir = Path(f"./build-cairo-v{cairo_version}-x{arch}")
    if not build_dir.exists():
        build_dir.mkdir()
    log.info("Using %s as build directory.", build_dir.absolute())

    if prefix is None:
        if sys.platform == "win32":
            prefix = Path(rf"C:\build-x{arch}")
        else:
            prefix = Path(f"~/build-x{arch}")
    log.info("Using %s as prefix", prefix)

    msvc = setup_vs(arch)

    root_dir = download_and_extract(
        DOWNLOAD_URL_CAIRO.format(
            CAIRO_VERSION=cairo_version,
            CAIRO_VERSION_STR=cairo_version,
            CAIRO_VERSION_MAJOR=cairo_version.split(".")[0],
            CAIRO_VERSION_MINOR=cairo_version.split(".")[1],
        ),
        build_dir,
        file_hash_sha256,
        check_file_hash,
    )
    subprojects_folder = Path(__file__).parent / "cairo-subprojects"
    log.info(f"Copy {subprojects_folder} to {root_dir / 'subprojects'}")
    if (root_dir / "subprojects").exists():
        shutil.rmtree(root_dir / "subprojects")
    shutil.copytree(subprojects_folder, root_dir / "subprojects")

    # Add patch to fix dwrite backend: to be removed in future version
    # see https://gitlab.freedesktop.org/cairo/cairo/-/merge_requests/302

    log.info("Patching Cairo sources...")
    apply_patch(
        patch_location=Path(__file__).parent / "302.patch",
        cwd=root_dir,
    )

    meson = get_meson_executable(build_dir)

    meson_build_dir = (root_dir / f"build-x{arch}").absolute()
    if meson_build_dir.exists():
        shutil.rmtree(meson_build_dir)

    # Static build is broken with Meson on Windows without these CFLAGS
    # See https://gitlab.freedesktop.org/cairo/cairo/-/issues/461
    # if sys.platform == "win32":
    #    ENVIRON["CFLAGS"] = "-DCAIRO_WIN32_STATIC_BUILD -DXML_STATIC"

    # Just so that meson doesn't try to link with system
    # stuff.
    ENVIRON["PKG_CONFIG_PATH"] = ""
    ENVIRON["PKG_CONFIG"] = "invalid-executable"

    run_meson(
        [
            meson,
            "setup",
            os.fspath(meson_build_dir),
            f"--default-library={build_type}",
            f"--prefix={prefix}",
            "--buildtype=release",
            "--wrap-mode=forcefallback",
            "-Dtee=enabled",  # this is needed for pycairo tests to pass
            "-Dglib=disabled",  # doesn't build statically in Windows
            "-Dtests=disabled",  # We don't need tests
        ],
        cwd=root_dir,
        env=ENVIRON,
    )

    log.info("Compiling now...")
    run_meson(
        [meson, "compile", "-C", os.fspath(meson_build_dir)],
        cwd=root_dir,
    )

    log.info("Installing Cairo.")
    run_meson(
        [
            meson,
            "install",
            "--no-rebuild",
            "-C",
            os.fspath(meson_build_dir),
        ]
    )

    # On MSVC, meson would create static libraries as
    # libcairo.a but setuptools doens't know about it.
    # So, we are copying every lib*.a to *.lib so that
    # setuptools can use it.
    if build_type == "static" and msvc:
        libreg = re.compile(r"lib(?P<name>\S*)\.a")
        libdir = prefix / "lib"
        for lib in libdir.glob("lib*.a"):
            name = libreg.match(lib.name).group("name") + ".lib"
            shutil.copyfile(lib, libdir / name)

    log.info("Sucessfully build Cairo")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--build-dir",
        required=False,
        type=Path,
        help="Build directory. (default: ./build*)",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        type=Path,
        help=f"Installation prefix. (default: {DEFAULT_PREFIX})",
    )
    parser.add_argument(
        "--cairo-version",
        default=DEFAULT_CAIRO_VERSION,
        help=f"Version of Cairo to build (default: {DEFAULT_CAIRO_VERSION})",
        type=str,
        dest="cairo_version",
    )
    parser.add_argument(
        "--pkgconf-version",
        default=DEFAULT_PKGCONF_VERSION,
        help=f"Version of pkgconf to build (default: {DEFAULT_PKGCONF_VERSION})",
        type=str,
        dest="pkgconf_version",
    )
    parser.add_argument(
        "--arch",
        default=get_python_arch(),
        help=f"Arch to build. (default: {get_python_arch()})",
        type=int,
    )
    parser.add_argument(
        "--check-file-hash",
        default=True,
        type=bool,
        help="Check file hash for files downloaded. (default: True)",
    )
    parser.add_argument(
        "--build-pkgconf",
        action=argparse.BooleanOptionalAction,
        help="Whether to build pkgconf. (default: False)",
        dest="build_pkgconf"
    )
    parser.add_argument(
        "--build-cairo",
        action=argparse.BooleanOptionalAction,
        help="Whether to build cairo. (default: True)",
        dest="build_cairo",
        default=True
    )
    op = parser.parse_args()
    if op.build_pkgconf:
        build_pkgconf(
            pkgconf_version=op.pkgconf_version,
            arch=op.arch,
            build_dir=op.build_dir,
            check_file_hash=op.check_file_hash,
            prefix=op.prefix.absolute(),
        )
    if op.build_cairo:
        build_cairo(
            cairo_version=op.cairo_version,
            arch=op.arch,
            build_dir=op.build_dir,
            check_file_hash=op.check_file_hash,
            prefix=op.prefix.absolute(),
        )
