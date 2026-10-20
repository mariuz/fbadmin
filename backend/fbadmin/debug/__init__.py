"""PSQL debugging help (milestone 1.2): variable trace runs, per-line profiler timings and saved tests.

Firebird has no debugger API. :mod:`.psql` rewrites a routine into an instrumented ``EXECUTE BLOCK``
that reports the line and the variables after each statement; :mod:`.runner` runs it in a
transaction that is rolled back; :mod:`.store` keeps test cases per routine.
"""
