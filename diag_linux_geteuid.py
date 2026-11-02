"""Diagnostic: why does the os.geteuid case not fire on Linux?

Prints the state the probe depends on, so the Linux CI can show it.
Run natively on Linux (not in the simulated probe).
"""
import os
import sys

print("=== 1. host ===")
print("   sys.platform:", sys.platform)
print("   os.name:", os.name)
print("   os.geteuid present before:", hasattr(os, "geteuid"))

print()
print("=== 2. does delattr stick? ===")
_real = os.geteuid
try:
    delattr(os, "geteuid")
    print("   delattr(os, 'geteuid') -> no exception")
except Exception as e:
    print("   delattr raised:", type(e).__name__, e)
print("   hasattr(os, 'geteuid') after delattr:", hasattr(os, "geteuid"))
try:
    os.geteuid()
    print("   os.geteuid() STILL WORKS (delattr did not stick)")
except AttributeError as e:
    print("   os.geteuid() raises AttributeError as expected:", e)

print()
print("=== 3. is 'os' in sys.modules the same object we mutated? ===")
print("   sys.modules['os'] is os:", sys.modules.get("os") is os)
print("   sys.modules['posix'] present:", "posix" in sys.modules)
print("   type(sys.modules.get('posix')):", type(sys.modules.get("posix")))

print()
print("=== 4. does a FRESH import of os get the mutated module? ===")
import importlib

fresh = importlib.import_module("os")
print("   importlib.import_module('os') is os:", fresh is os)
print("   fresh has geteuid:", hasattr(fresh, "geteuid"))

print()
print("=== 5. is os a pure-Python module or the builtin? ===")
print("   os.__file__:", getattr(os, "__file__", None))
print("   os.__spec__.origin:", getattr(getattr(os, "__spec__", None), "origin", None))

print()
print("=== 6. what does a geteuid-referencing module see? ===")
_src = (
    "import os\n"
    "import pytest\n"
    "\n"
    "\n"
    "class T:\n"
    '    @pytest.mark.skipif(os.geteuid() == 0, reason="root")\n'
    "    def test_x(self):\n"
    "        pass\n"
)
_ns = {"__name__": "_probe_mod"}
try:
    exec(compile(_src, "<probe>", "exec"), _ns)
    print("   compiled OK -> os.geteuid() did NOT raise")
except AttributeError as e:
    print("   AttributeError as expected:", e)
except BaseException as e:
    print("   other exception:", type(e).__name__, e)

print()
print("=== 7. restore and confirm ===")
os.geteuid = _real
print("   restored:", hasattr(os, "geteuid"))
print("DONE")
