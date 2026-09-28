import sys
sys.path.insert(0, 'src')
sys.path.insert(0, '.')

import pytest
sys.argv = ['pytest', 'tests/test_sources_collect.py', '-v']
exit_code = pytest.main()
sys.exit(exit_code)