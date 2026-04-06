PYTHON = python3
DEFAULT_INSTALL_DIR = $(HOME)/.local/share/ai-agent/copilot
INSTALL_DIR ?= $(DEFAULT_INSTALL_DIR)
INSTALL_DIR_STATE ?= $(HOME)/.local/share/ai-agent/copilot-install-dir
VENV_DIR = .venv
VENV_PYTHON = $(VENV_DIR)/bin/python
VENV_PIP = $(VENV_DIR)/bin/pip
TEST_VENV_DIR = .venv_test
TEST_PYTHON = $(TEST_VENV_DIR)/bin/python
TEST_PIP = $(TEST_VENV_DIR)/bin/pip
TMP_DIR = .tmp

.PHONY: help bootstrap venv test-venv install-dev run-server install update status \
	uninstall lint lint-python lint-shell test test-python test-wrapper test-installer \
	test-shell check clean distclean

help:
	@printf "%s\n" \
		"Available targets:" \
		"  make venv          - create/update .venv with runtime dependencies" \
		"  make test-venv     - create/update .venv_test with test dependencies" \
		"  make bootstrap     - prepare both virtual environments" \
		"  make run-server    - run the MCP server from the repo checkout" \
		"  make install       - run install-copilot-agent.sh" \
		"  make update        - run install-copilot-agent.sh --update" \
		"  make status        - run install-copilot-agent.sh --status" \
		"  make uninstall     - remove the installed files and Claude MCP registration" \
		"  make lint          - shell syntax checks + Python bytecode checks" \
		"  make test          - run Python and shell tests" \
		"  make check         - run lint + all tests" \
		"  make clean         - remove caches and temporary files" \
		"  make distclean     - clean + remove local virtual environments"

venv:
	@if [ ! -x "$(VENV_PYTHON)" ]; then \
		"$(PYTHON)" -m venv "$(VENV_DIR)"; \
	fi
	@if [ ! -x "$(VENV_PIP)" ]; then \
		"$(VENV_PYTHON)" -m ensurepip --upgrade; \
	fi
	@"$(VENV_PIP)" install --upgrade pip
	@"$(VENV_PIP)" install -e .

test-venv:
	@if [ ! -x "$(TEST_PYTHON)" ]; then \
		"$(PYTHON)" -m venv "$(TEST_VENV_DIR)"; \
	fi
	@if [ ! -x "$(TEST_PIP)" ]; then \
		"$(TEST_PYTHON)" -m ensurepip --upgrade; \
	fi
	@"$(TEST_PIP)" install --upgrade pip
	@"$(TEST_PIP)" install -e ".[test]"

bootstrap: venv test-venv

install-dev: bootstrap

run-server:
	@if [ ! -x "$(VENV_PYTHON)" ]; then \
		echo "Missing $(VENV_PYTHON). Run 'make venv' first."; \
		exit 1; \
	fi
	@PYTHONPATH=src "$(VENV_PYTHON)" src/copilot_mcp_server.py

install:
	@bash ./install-copilot-agent.sh

update:
	@bash ./install-copilot-agent.sh --update

status:
	@bash ./install-copilot-agent.sh --status

uninstall:
	@install_dir="$(INSTALL_DIR)"; \
	if [ "$$install_dir" = "$(DEFAULT_INSTALL_DIR)" ] && [ -f "$(INSTALL_DIR_STATE)" ]; then \
		IFS= read -r saved_dir < "$(INSTALL_DIR_STATE)" || saved_dir=""; \
		if [ -n "$$saved_dir" ]; then \
			install_dir="$$saved_dir"; \
		fi; \
	fi; \
	if command -v claude >/dev/null 2>&1; then \
		claude mcp remove -s user copilot-delegate >/dev/null 2>&1 || true; \
		claude mcp remove -s project copilot-delegate >/dev/null 2>&1 || true; \
		claude mcp remove -s local copilot-delegate >/dev/null 2>&1 || true; \
		claude mcp remove copilot-delegate >/dev/null 2>&1 || true; \
	fi; \
	if [ -d "$$install_dir" ]; then \
		rm -rf "$$install_dir"; \
		printf '%s\n' "Removed $$install_dir"; \
	else \
		printf '%s\n' "Nothing to remove at $$install_dir"; \
	fi; \
	if [ -f "$(INSTALL_DIR_STATE)" ]; then \
		IFS= read -r saved_dir < "$(INSTALL_DIR_STATE)" || saved_dir=""; \
		if [ "$$saved_dir" = "$$install_dir" ]; then \
			rm -f "$(INSTALL_DIR_STATE)"; \
		fi; \
	fi

lint: lint-python lint-shell

lint-python:
	@"$(PYTHON)" -m py_compile src/copilot_mcp_server.py src/redact.py

lint-shell:
	@bash -n src/copilot_wrapper.sh install-copilot-agent.sh tests/test_wrapper.sh tests/test_installer.sh

test: test-python test-shell

test-python:
	@if [ ! -x "$(TEST_PYTHON)" ]; then \
		echo "Missing $(TEST_PYTHON). Run 'make test-venv' first."; \
		exit 1; \
	fi
	@"$(TEST_PYTHON)" -m pytest -q

test-wrapper:
	@mkdir -p "$(TMP_DIR)"
	@COPILOT_INSTALL_DIR="$$(pwd)/$(TMP_DIR)/wrapper-tests" bash tests/test_wrapper.sh

test-installer:
	@bash tests/test_installer.sh

test-shell: test-wrapper test-installer

check: lint test

clean:
	@rm -rf "$(TMP_DIR)" .pytest_cache
	@find . -depth -type d -name "__pycache__" -exec rm -rf {} +
	@find . -type f \( -name "*.pyc" -o -name "*.pyo" \) -exec rm -f {} +

distclean: clean
	@rm -rf "$(VENV_DIR)" "$(TEST_VENV_DIR)"
