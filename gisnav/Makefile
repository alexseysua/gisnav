SHELL := /bin/bash

# test
.PHONY: test-sitl
test-sitl:
	@python gisnav/test/sitl/test_sitl.py px4 \
		|| (echo "SITL test failed, exit code: $$?"; exit 1)

.PHONY: test-launch
test-launch:
	@launch_test gisnav/test/launch/test_default_launch.py

.PHONY: test-unit
test-unit:
	@python3 -m unittest discover -s gisnav/test/unit -p "test_*.py"

.PHONY: test-static
test-static:
	@pre-commit run --all-files
# test end
