import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from cpplcom.cli import main

HEADER = """
#pragma once
#include <functional>
#include <vector>

struct Base { virtual ~Base() = default; virtual int kind() const = 0; void baseCall() const {} };

class Split : public Base {
public:
  Split() : x(1), y(2) {}
  Split& operator=(const Split& o) { x = o.x; y = o.y; return *this; }
  int a() const { return x; }
  int b() const { return x + 1; }
  int c() const { baseCall(); return y; }
  int d() const;
  static int s() { return 0; }
  int kind() const override { return 3; }
private:
  int x, y;
};

class Joined {
public:
  int a() { return x; }
  int b() { return y + a(); }
private:
  int x = 0, y = 0;
};

template <typename T> class Tmpl {
public:
  template <typename U> void put(U u) { this->items.push_back(T(u)); }
  void twice(T t) { put(t); put(t); }
  int size() const { auto f = [this] { return (int)items.size(); }; return f(); }
private:
  std::vector<T> items;
};

struct Updater { virtual ~Updater() = default; void update() { apply(); } virtual void apply() = 0; };

class Dispatched : public Updater {
public:
  void resume() { info = 1; update(); }
  void apply() override { gain = 2; }
  int process() { return gain; }
private:
  int info = 0, gain = 0;
};

class Host {
public:
  void close() { _hide(); }
  void grow() { _size += 1; }
  int size() { return _size; }
private:
  std::function<void()> _hide;
  int _size = 0;
};

class Writer {
public:
  void set() { _v.push_back(1); }
  int other() { return _w = 2; }
private:
  std::vector<int> _v;
  int _w = 0;
};

struct Root { virtual ~Root() = default; void update() { apply(); } virtual void apply() = 0; };
struct Middle : Root { int middle = 0; };

class Deep : public Middle {
public:
  void resume() { info = 1; update(); }
  void apply() override { gain = 2; }
  int process() { return gain; }
private:
  int info = 0, gain = 0;
};

struct Stepper { virtual ~Stepper() = default; void run() { step(); } virtual void step() { tick(); } virtual void tick() {} };

class Overridden : public Stepper {
public:
  void start() { _a = 1; run(); }
  void step() override { _a = 2; }
  void tick() override { _b = 1; }
  int b() { return _b; }
private:
  int _a = 0, _b = 0;
};

struct Ticker { virtual ~Ticker() = default; virtual void tick() {} };

class Qualified : public Ticker {
public:
  void start() { _a = 1; Ticker::tick(); }
  void tick() override { _b = 1; }
  int b() { return _b; }
private:
  int _a = 0, _b = 0;
};

class ExplicitSelf {
public:
  void run() { helper(*this, 2); }
  void step(int n) { _state += n; }
  void step() { _other = 0; }
  int other() { return _other; }
  template <class SELF>
  static void helper(SELF& self, int n) { self.step(n); }
private:
  int _state = 0, _other = 0;
};
"""

SOURCES = {
    "classes.hpp": HEADER,
    "split.cpp": '#include "classes.hpp"\nint Split::d() const { return y * 2; }\n',
    "use.cpp": '#include "classes.hpp"\nint use() { Tmpl<int> t; t.twice(1); return t.size(); }\n',
    "vendor/lib.cpp": "struct Vendored { int a() { return x; } int b() { return y; } int x, y; };\n",
}


@unittest.skipUnless(shutil.which("clang++"), "needs clang++ and its libclang")
class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for name, text in SOURCES.items():
            path = os.path.join(self.dir, "src", name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        self.database = os.path.join(self.dir, "compile_commands.json")
        with open(self.database, "w") as f:
            json.dump([{"directory": self.dir, "file": f"src/{name}",
                        "arguments": ["clang++", "-std=c++20", "-c", f"src/{name}", "-o", f"{name}.o"]}
                       for name in SOURCES if name.endswith(".cpp")], f)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_cli(self, *extra):
        out = os.path.join(self.dir, "out.json")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main_with(["--compile-commands", self.database, "--root", os.path.join(self.dir, "src"),
                              "--json", out, *extra])
        with open(out) as f:
            return code, {row["class"]: row for row in json.load(f)}, stdout.getvalue()

    def test_scores(self):
        code, rows, _ = self.run_cli()
        jobs = {name: (row["jobs"], row["lcom4"], row["bare"]) for name, row in rows.items()}
        self.assertEqual(jobs["Split"], (2, 3, 1), "constructor and assignment do not join; kind() is bare")
        self.assertEqual(jobs["Joined"], (1, 1, 0), "a call joins")
        self.assertEqual(jobs["Tmpl"], (1, 1, 0), "this->, a member template and a lambda join")
        self.assertEqual(jobs["Dispatched"], (1, 1, 0), "a base call joins the overrides it reaches")
        self.assertEqual(jobs["Host"], (1, 2, 1), "a lone method using fields only through const calls is bare")
        self.assertEqual(jobs["Writer"], (2, 2, 0), "a lone method that writes is a job")
        self.assertEqual(jobs["ExplicitSelf"], (2, 2, 0), "self.step(n) joins step(int), not step()")
        self.assertEqual(jobs["Deep"], (1, 1, 0), "dispatch is followed through a base with no methods of its own")
        self.assertEqual(jobs["Overridden"], (2, 2, 0), "an overridden base body never runs, so its calls join nothing")
        self.assertEqual(jobs["Qualified"], (2, 2, 0), "Base::tick() runs the base body, not the override")
        self.assertIn(["c", "d"], rows["Split"]["components"], "an out-of-line definition is the same method")
        self.assertNotIn("Vendored", rows, "vendor/ is excluded by default")
        self.assertEqual(code, 1)

    def test_threshold(self):
        code, _, report = self.run_cli("--threshold", "2")
        self.assertEqual(code, 0)
        self.assertIn("0 over the threshold", report)


def main_with(argv):
    import sys

    saved, sys.argv = sys.argv, ["cpplcom", *argv]
    try:
        return main()
    finally:
        sys.argv = saved


if __name__ == "__main__":
    unittest.main()
