# Contributing

Small, focused pull requests are welcome. Please keep it stdlib-only (no new
dependencies), run `python3 -m unittest discover -s tests -t .` before you push, and use
fakes in tests: never real hardware, real captures or real tokens.

Features that capture, decode or replay rolling codes, or that let a request choose a
file path, will not be merged.
