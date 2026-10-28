# cpplcom

LCOM4 for C++. It finds the classes that do more than one job: groups of methods that share no state and never call
each other, living under one name.

```
LCOM4 report  (threshold 1 job per class)

 JOBS  LCOM4  BARE  METHODS  FIELDS  CLASS
    2      3     1        5       2  Split
          src/split.hpp:5
          {a, b}
          {c, d}
          {kind}

  6 classes with 2+ methods, 1 over the threshold; by jobs: 1: 5, 2: 1
```

LCOM4 is Hitz & Montazeri's lack of cohesion of methods. A class's methods are the nodes of a graph; two are joined
when both use the same field or one calls the other. LCOM4 is the number of separate parts. Each brace in the report
is one part.

Exit code is 1 when a class has more jobs than `--threshold` (default 1), or when a file does not parse, so it works
as a build gate.

## What counts

- **Methods:** the class's own non-static member functions with a body, wherever the body is defined. Constructors,
  destructors and copy/move assignment are left out: they touch every field by design and would join everything.
  Defaulted, deleted and pure virtual members have no body.
- **Fields:** the class's own non-static data members. Inherited fields join nothing.
- **Calls into a base class** join the caller to the class's own overrides of every virtual the base method reaches:
  `resume()` calling `Base::update()`, which calls the virtual `apply()`, joins `resume` to this class's `apply`.
- **Static members on an explicit object** (`template <class SELF> static void f(SELF& self, ...)`, the pre-C++23
  way to call `self.process()` without a virtual call) are methods. `self.member` is dependent, so it is matched by
  name, and a call by its argument count.
- **Templates** are scored once, from their own definition. A lambda counts toward the method it is written in.

**Jobs** are LCOM4 minus the **BARE** parts: a single method that uses no field (an override returning a constant),
or one that only reads its fields, being `const` or using them only through `const` calls. Alone in its part, such a
method reads fields no other method touches, so they are constructor state, and constructors are not in the graph.
A getter for a flag set at construction is not a second job.

## Install

```sh
pip install -e .
```

It needs the libclang Python bindings (`clang` on PyPI, pure Python) and a libclang library. It loads the libclang
that belongs to the compiler in your compile database (`<compiler> -print-resource-dir`, two levels up), and takes
that compiler's system include paths, so sources parse exactly as they build. Any recent bindings work against it.

## Use

```sh
cpplcom --compile-commands build/compile_commands.json --root src
cpplcom --compile-commands build/compile_commands.json --root src --exclude '*.test.*' --json lcom.json
python -m cpplcom ...       # without installing: PYTHONPATH=<checkout>:<clang wheel>
```

Run it after a build if any source includes generated headers. `ext/`, `vendor/` and `third_party/` are excluded by
default; `--exclude` adds globs on absolute paths, which also skip whole translation units. `--threshold` raises the
bar while paying down an area.

### CMake

Pinned like any other dependency, with the bindings wheel imported straight from `PYTHONPATH`, nothing installed:

```cmake
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

CPMAddPackage(NAME cpplcom GITHUB_REPOSITORY ulfhammarqvist-st/cpplcom GIT_TAG <sha> DOWNLOAD_ONLY YES)

set(CLANG_WHEEL ${CMAKE_BINARY_DIR}/clang-19.1.7-py3-none-any.whl)
file(DOWNLOAD
  https://files.pythonhosted.org/packages/d9/5e/f11955a953db4476fa98ca3e28ac9fdef063a44d1cf749b6f8766bb2398c/clang-19.1.7-py3-none-any.whl
  ${CLANG_WHEEL}
  EXPECTED_HASH SHA256=a5d0931222e0c96a9933d942065d970859820482ee16854b7dfa1e7e2033fab3)

add_custom_target(lcom
  COMMAND ${CMAKE_COMMAND} -E env PYTHONPATH=${cpplcom_SOURCE_DIR}:${CLANG_WHEEL}
          python3 -m cpplcom --compile-commands ${CMAKE_BINARY_DIR}/compile_commands.json --root ${PROJECT_SOURCE_DIR}/src
  USES_TERMINAL)
```

## Reading it

Two jobs means two groups of methods that share no state and never call each other. The way out is to split the
class, or to move the second group to the class whose state it really uses.

Only what the build compiles is seen: code behind a platform guard is scored on the platforms that compile it, so a
clean run on one platform says nothing about another. Score the build your CI gates on.

## Tests

```sh
PYTHONPATH=<clang wheel>:. python3 -m unittest
```

They compile small fixtures with the `clang++` on `PATH` and skip without one.
