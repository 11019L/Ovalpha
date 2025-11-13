# fix_jupiter.py
import os
import sys

# Patch jupiter_python_sdk to use correct submodule
sdk_path = os.path.join(os.path.dirname(sys.executable), '..', 'site-packages', 'jupiter_python_sdk', '__init__.py')

if os.path.exists(sdk_path):
    content = open(sdk_path, 'r').read()
    if 'from .jupiter import Jupiter' not in content:
        with open(sdk_path, 'a') as f:
            f.write('\nfrom .jupiter import Jupiter\n')
        print("Patched jupiter_python_sdk.__init__.py")
else:
    print("Warning: jupiter_python_sdk not found – install may have failed")
