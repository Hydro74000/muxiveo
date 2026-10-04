#!/usr/bin/env python3
"""Construit depuis les sources épinglées, sans bindings Python dans le runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path | None = None) -> None:
    """Commande de construction explicite, sans interpréteur shell."""
    print("+ " + " ".join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, check=True)


def source(name: str, spec: dict, cache: Path) -> Path:
    """Vérifie l'archive avant extraction dans le cache de construction."""
    dest = cache / name
    archive = cache / (name + ".tar.gz")
    if not archive.is_file():
        with urllib.request.urlopen(spec["url"], timeout=120) as response:
            archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != spec["sha256"]:
        raise RuntimeError(f"SHA256 incorrect : {name}")
    if dest.is_dir():
        return dest
    with tarfile.open(archive) as tf:
        tf.extractall(cache / (name + "-extract"), filter="data")
    extracted = cache / (name + "-extract")
    children = list(extracted.iterdir())
    shutil.move(str(children[0]), dest)
    extracted.rmdir()
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=Path("build/muxiveo-mvtools"))
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    build = args.build_dir.resolve()
    cache = build / "sources"
    prefix = build / "prefix"
    cache.mkdir(parents=True, exist_ok=True)
    specs = json.loads((ROOT / "dependencies.json").read_text())
    sources = {name: source(name, spec, cache) for name, spec in specs.items()}
    vs, mv = sources["vapoursynth"], sources["mvtools"]
    meson = shutil.which("meson")
    if not meson:
        raise RuntimeError("Meson requis pour construire VapourSynth et MVTools")
    jobs = str(max(1, args.jobs))
    if os.name == "nt":
        compiler = shutil.which("cl.exe")
        if not compiler:
            raise RuntimeError("Environnement MSVC x64 requis")
        # Git Bash expose aussi un link.exe GNU : préférer le linker de MSVC.
        os.environ["PATH"] = str(Path(compiler).parent) + os.pathsep + os.environ["PATH"]
    os.environ["PKG_CONFIG_PATH"] = str(prefix / "lib/pkgconfig") + os.pathsep + os.environ.get("PKG_CONFIG_PATH", "")
    os.environ.setdefault("MACOSX_DEPLOYMENT_TARGET", "12.0")

    # zimg 3.0.6 : noyaux scalaires portables ; MVTools ne fait aucun resize zimg.
    zimg = sources["zimg"]
    if not (zimg / "CMakeLists.txt").exists():
        units = sorted(p.relative_to(zimg).as_posix() for p in (zimg / "src/zimg").rglob("*.cpp")
                       if "/x86/" not in p.as_posix() and "/arm/" not in p.as_posix())
        (zimg / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.20)\nproject(zimg LANGUAGES CXX)\n"
            "set(CMAKE_CXX_STANDARD 17)\nset(CMAKE_POSITION_INDEPENDENT_CODE ON)\n"
            "if(MSVC)\nadd_compile_definitions(_USE_MATH_DEFINES NOMINMAX)\nendif()\n"
            "add_library(zimg STATIC " + " ".join(units) + ")\n"
            "target_include_directories(zimg PRIVATE src/zimg)\n"
            "install(TARGETS zimg ARCHIVE DESTINATION lib)\n"
            "install(FILES src/zimg/api/zimg.h src/zimg/api/zimg++.hpp DESTINATION include)\n"
        )
    # Cache de construction antérieur à l'ajout du wrapper C++ de zimg.
    shutil.copy2(zimg / "src/zimg/api/zimg++.hpp", prefix / "include/zimg++.hpp") if (prefix / "include").is_dir() else None
    for name, opts in (("zimg", []), ("fftw", ["-DENABLE_FLOAT=ON", "-DBUILD_SHARED_LIBS=OFF", "-DBUILD_TESTS=OFF", "-DENABLE_FORTRAN=OFF"])):
        target = build / (name + "-build")
        run("cmake", "-S", str(sources[name]), "-B", str(target), "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
            "-DCMAKE_INSTALL_PREFIX=" + str(prefix), "-DCMAKE_INSTALL_LIBDIR=lib",
            "-DCMAKE_POLICY_DEFAULT_CMP0091=NEW", "-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded", *opts)
        run("cmake", "--build", str(target), "--parallel", jobs)
        run("cmake", "--install", str(target))
    pc = prefix / "lib/pkgconfig"
    pc.mkdir(parents=True, exist_ok=True)
    for name, version, lib in (("zimg", "3.0.6", "zimg"), ("fftw3f", "3.3.11", "fftw3f")):
        (pc / (name + ".pc")).write_text(
            f"prefix={prefix.as_posix()}\nlibdir=${{prefix}}/lib\nincludedir=${{prefix}}/include\n"
            f"Name: {name}\nDescription: runtime natif Muxiveo\nVersion: {version}\n"
            f"Libs: -L${{libdir}} -l{lib}\nCflags: -I${{includedir}}\n"
        )

    # Modification reproductible de la construction amont, jamais de son calcul.
    text = (vs / "meson.build").read_text()
    if "is_freethreaded =" in text:
        text = text.replace("'c', 'cpp', 'cython'", "'c', 'cpp'")
        text = text.replace("    libp2p_dep = dependency('libp2p')\n", "")
        text = text.replace("py_dep = py.dependency(version: is_win ? '>=3.14' : '>=3.12')  # Py_NO_LINK_LIB was introduced in 3.14", "")
        start = text.index("is_freethreaded =")
        end = text.index("# --- libvapoursynthfilters", start)
        text = text[:start] + text[end:]
        text = text.replace("{ 'name': 'libvapoursynthfilters_zn4',    'tag': 'znver4',   'level': 3 },", "")
        text = text.replace("dependency('zimg', version: '>=3.0.5')", "dependency('zimg', version: '>=3.0.5', static: true)")
        # En-têtes Vulkan internes épinglés par R80, jamais ceux du SDK du runner.
        start = text.index("vulkan_headers = dependency('vulkan'")
        end = text.index("\nlibvapoursynth =", start)
        text = text[:start] + "vulkan_headers = subproject('vulkan-headers').get_variable('vulkan_headers_dep')\n" + text[end:]
        (vs / "meson.build").write_text(text)
    text = (mv / "meson.build").read_text()
    if "import vapoursynth as vs" in text:
        start = text.index("incdir = include_directories(")
        end = text.index("\nlibs =", start)
        text = text[:start] + "incdir = include_directories('" + (vs / "include").as_posix() + "')\n" + text[end:]
        text = text.replace("dependency('fftw3f')", "dependency('fftw3f', static: true)")
        # clang-cl et MSVC n'acceptent pas -march comme option native.
        text = text.replace("cpp_args: '-march=x86-64-v3'", "cpp_args: meson.get_compiler('cpp').get_argument_syntax() == 'msvc' ? '/arch:AVX2' : '-march=x86-64-v3'")
        (mv / "meson.build").write_text(text)
    for name, src in (("vs", vs), ("mv", mv)):
        target = build / (name + "-build")
        command = [meson, "setup", str(target), str(src), "--buildtype=release", "--prefix=" + str(prefix)]
        if os.name == "nt":
            command += ["-Db_vscrt=mt"]
        elif sys.platform.startswith("linux"):
            command += ["-Dcpp_link_args=['-static-libstdc++','-static-libgcc']"]
        if (target / "build.ninja").is_file():
            command += ["--reconfigure"]
        run(*command)
        run(meson, "compile", "-C", str(target), "-j", jobs)
    run("cmake", "-S", str(ROOT), "-B", str(build / "tool"), "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release", "-DMUXIVEO_MVTOOLS_VS_INCLUDE=" + str(vs / "include"))
    run("cmake", "--build", str(build / "tool"), "--parallel", jobs)
    bundle = build / "bundle"
    bundle.mkdir(exist_ok=True)
    run("cmake", "--install", str(build / "tool"), "--prefix", str(bundle))
    runtime = bundle / "mvtools-runtime"
    runtime.mkdir(exist_ok=True)
    ext = ".dll" if os.name == "nt" else ".dylib" if sys.platform == "darwin" else ".so"
    for name, target in (("libvapoursynth", "vs"), ("libvapoursynthfilters", "vs"), ("libvapoursynthfilters_avx2", "vs"), ("mvtools", "mv")):
        candidates = sorted((build / (target + "-build")).glob(name + ext + "*"))
        if not candidates and sys.platform == "darwin":
            candidates = sorted((build / (target + "-build")).glob(name + "*.dylib"))
            if not candidates:
                candidates = sorted((build / (target + "-build")).glob(name + ".so"))
        if candidates:
            shutil.copy2(candidates[0].resolve(), runtime / (name + ext))
        elif name != "libvapoursynthfilters_avx2" or platform.machine().lower() in ("x86_64", "amd64"):
            raise RuntimeError(f"Bibliothèque absente : {name}")
    if sys.platform.startswith("linux"):
        for lib in runtime.glob("*.so"):
            run("patchelf", "--set-rpath", "$ORIGIN", str(lib))
    if os.name != "nt":
        for path in [bundle / "muxiveo-mvtools", *runtime.glob("*" + ext)]:
            run("strip", "-x" if sys.platform == "darwin" else "--strip-unneeded", str(path))
    licenses = runtime / "licenses"
    licenses.mkdir(exist_ok=True)
    for name, src in sources.items():
        for pat in ("LICENSE*", "COPYING*", "Copyright*"):
            for path in src.glob(pat):
                if path.is_file():
                    shutil.copy2(path, licenses / (name + "-" + path.name))
    for src in (vs / "subprojects").iterdir():
        if src.is_dir():
            for pat in ("LICENSE*", "COPYING*"):
                for path in src.glob(pat):
                    if path.is_file():
                        shutil.copy2(path, licenses / (src.name + "-" + path.name))
    shutil.copy2(ROOT / "LICENSE", licenses / "muxiveo-mvtools-GPL-3.0.txt")
    shutil.copy2(ROOT.parent.parent / "LICENSE", licenses / "Muxiveo-MIT.txt")
    internal = {}
    for subproject in (vs / "subprojects").iterdir():
        if (subproject / ".git").exists():
            result = subprocess.run(["git", "-C", str(subproject), "rev-parse", "HEAD"],
                                    capture_output=True, text=True, check=True)
            internal[subproject.name] = result.stdout.strip()
    manifest = {"version": "1.0.0", "dependencies": specs, "vapoursynth_internal_revisions": internal, "files": {}}
    for path in sorted(bundle.rglob("*")):
        if path.is_file():
            manifest["files"][path.relative_to(bundle).as_posix()] = {
                "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
    (runtime / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    exe = bundle / ("muxiveo-mvtools.exe" if os.name == "nt" else "muxiveo-mvtools")
    run(str(exe), "--self-test", "--json")
    print("Installed bytes:", sum(p.stat().st_size for p in bundle.rglob("*") if p.is_file()))


if __name__ == "__main__":
    main()
